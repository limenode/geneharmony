# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An async Python wrapper around the [Alliance of Genome Resources](https://www.alliancegenome.org) (AGR) REST API and its bulk-download files. It resolves gene symbols/IDs to canonical genes with an in-memory index built from AGR's `GENE-TSV-COMBINED` bulk file, fetches API data concurrently, and downloads/parses bulk files. `Annotator` (`annotator.py`) is the user-facing surface — `download` / `ingest_annotation` / `annotate`. The pipeline is developed interactively in `src/notebook.ipynb`; `src/main.py` is still an empty placeholder.

This replaces an earlier design (kept in `src_old/` for reference) that used an external Rust `gene_normalizer` binary and a filesystem cache of per-endpoint DataFrames. Gene normalization is now pure Python against the bulk file — there is **no external binary and no `.env`** to set up (`.env.example` is stale).

## Environment & commands

The package is **pip-installable** (`pip install geneharmony`); runtime dependencies live in `[project.dependencies]` in `pyproject.toml` (`httpx`, `pydantic` v2, `pandas` 3.x, `pyarrow`) and are the single source of truth for end users. **pixi** (conda-forge) is the **development** environment only — end users don't need it. There are **no defined tasks, tests, or linters** — `[tasks]` in `pixi.toml` is empty. Published floor is Python 3.12+ (`requires-python`); the pixi dev env runs 3.14 (free-threaded).

```bash
pixi install                                # create the environment from pixi.lock
pixi run python <script>                    # run Python inside the env
pixi run jupyter lab src/notebook.ipynb     # open the driver notebook
```

**Notebook outputs are stripped on commit** via a git clean filter (`*.ipynb filter=nbstrip` in `.gitattributes`), keeping `notebook.ipynb` diffs to code only. The filter is repo-local config, so enable it once per clone:

```bash
git config filter.nbstrip.clean "pixi run jupyter nbconvert --clear-output --to notebook --stdin --stdout --log-level=ERROR"
git config filter.nbstrip.smudge cat
```

**Imports are flat** (`from client import ...`, `from normalizer import ...`) with no package prefix, so code only resolves when **`src/` is the working directory / on `sys.path`**. The notebook runs there; standalone scripts must too (e.g. `sys.path.insert(0, "src")`).

## Conventions

The owner prefers **strong typing** (modern `type` aliases, `Final`, `Self`, `enum`, `NamedTuple`) and **minimal comments** — comments are for user-facing docstrings or genuinely non-obvious logic, not narration. Match this when editing.

## Architecture

Two AGR hosts are in play, and keeping them separate matters:
- **API host** `https://www.alliancegenome.org/api` — JSON endpoints, served by `AGRClient`.
- **Download host** `https://download.alliancegenome.org` — large bulk files, served by `Downloader`. The `/downloads` *listing* is JSON on the API host; only the file bytes come from the download host (the `s3Url` field).

### `client.py` — `AGRClient`

Async API client wrapping one pooled `httpx.AsyncClient` bounded by an `asyncio.Semaphore` (default `max_concurrent=5`). `get_json` / `get_text` issue GETs through `_get`, which **retries** transient failures (statuses 429/502/503/504, timeouts, transport errors) with full-jitter exponential backoff, honoring `Retry-After`. `list_downloads()` fetches `/downloads` and validates it into `list[DownloadFile]` via a module-level `TypeAdapter`. Async context manager; `aclose()` closes the pool.

### `downloader.py` — `Downloader`

Host-agnostic streaming file downloader (deliberately **not** AGR-specific — usable for any absolute URL). `download(url, dest, *, expected_size=None)` streams to disk in 1 MiB chunks via `client.stream(...)`, writes to a `.part` temp file then `os.replace`s into place (atomic; `dest` only ever exists complete). Bytes are written **verbatim** — `.gz` stays compressed on disk and is inflated at ingest. Retries transport/transient-status errors with the same backoff style as the client. Raises `SizeMismatchError` on a post-download size check.

> Caveat: the `/downloads` listing's `size` is the **uncompressed** size, but we fetch the compressed `.gz`. So do **not** pass `expected_size=DownloadFile.size` — it will always mismatch. The skip-if-already-downloaded shortcut only engages when `expected_size` is given, so it's currently a no-op for these files.

### `ingest.py` — decompress + parse

Free functions, no class. Read straight from the compressed file into memory (no decompressed copy on disk):
- `load_json_gz(path) -> Any` — `gzip.open` + `json.load`.
- `load_tsv_gz(path, dtype=None) -> pd.DataFrame` — `pd.read_csv(sep="\t", comment="#", compression="gzip")`. The `comment="#"` skips AGR's leading metadata block; `dtype=str` is needed for the gene file so digit-like symbols/IDs aren't coerced to numbers.

Note: `sep="\t"` in real source files uses pandas' fast C engine. (A spurious "falling back to the python engine / regex separator" warning only appears when `\t` is passed through a shell `-c` invocation, where it arrives as a literal two-char `\t` — not a real-code problem.)

### `normalizer.py` — the gene index

`load_gene_index(path) -> GeneIndex` reads `GENE-TSV-COMBINED` (`dtype=str`, all columns) and precomputes O(1) lookups. The file is ~914k rows across 9 species; the real ID column is **`GeneId`** (not `GeneID`); `GeneSymbol` is **not unique** even within a species; `GeneSecondaryIds` are deprecated IDs.

`build_gene_index` fills five `dict[str, list[int]]` tables mapping each identifier form to row positions in the retained `records` DataFrame, tagged by `MatchKind`:

```
PRIMARY_ID  >  SECONDARY_ID  >  OFFICIAL_SYMBOL  >  SYNONYM  >  CROSS_REFERENCE   (precedence, high→low)
```

**Precedence is the `MatchKind` enum's definition order**, surfaced by `for kind in MatchKind` in `_resolve` (there is no explicit sort; the int values are documentation only). Keep members declared best-to-worst.

`CROSS_REFERENCE` indexes `GeneCrossReferences` — pipe-separated external IDs (`NCBI_Gene:`, `ENSEMBL:`, `UniProtKB:`, `RefSeq:`, …), populated on ~43% of rows. Keys are the **full `PREFIX:ID` token**, not the bare ID (bare IDs collide across databases — e.g. `601309` is both OMIM and MIM). It is the **lowest** tier, so cross-refs only resolve queries no better identifier matches. Family/class databases that fan one token out to hundreds of genes (`_XREF_EXCLUDED_PREFIXES`: `PANTHER`, `TreeFam`, `ExPASy`, `TCDB`) are **excluded**. Some `RGD:*` tokens also appear in `GeneSecondaryIds`; the higher `SECONDARY_ID` tier wins, so the duplication is harmless.

`GeneIndex.lookup(queries, *, taxon=None, limit=1, case_insensitive=False) -> pd.DataFrame`:
- `queries`: `str | list[str]` (scalar is wrapped). Built for batches of thousands.
- Returns one row per `(query, match)`: columns `query`, `match_kind`, then **all** gene-record columns. Input order preserved.
- **Unmatched queries are retained** with null `match_kind` (and `NaN` record cells) so misses are visible. Filter with `df.match_kind.notna()`.
- `limit` caps matches per query (`limit=None` = all → useful for top-N when no taxon disambiguates). Pipeline is **precedence-sort → taxon-filter → dedup → limit** (i.e. filter-then-limit). A kind filter, if ever wanted, is left to the caller on the returned frame — note that user-side kind-filtering happens *after* `limit`.
- Within a tier, matches are ordered by **row/file order** (not species priority).
- `case_insensitive=True` consults a **lazily-built** casefolded copy of the tables (zero cost otherwise). Default is case-sensitive, since case can be meaningful across species (human `TP53` vs mouse `Trp53`).

### `taxa.py` — taxon resolution (`taxa.json` + `resolve_taxon`)

Self-contained species resolution with no dependency on the gene index (not even pandas); `normalizer.py` and `annotator.py` both import `resolve_taxon` from here. `taxa.json` (in `src/`, read via a path relative to `taxa.py`) holds one entry per species with `id` / `species` / `common`. At import, `_load_taxa` builds the `_TAXA` tuple of `Taxon` records, then `_TAXON_BY_ALIAS` maps every casefolded string tied to a species — full `NCBITaxon:` ID, bare number, species name, each common name — back to its `Taxon`. The full ID is itself an alias, so this one map covers taxon-ID lookups too (no separate by-ID table). `Taxon` is a `NamedTuple` (`id`, `species`, `common`) with `.number` (bare ID, no prefix) and `.common_name` (first common name, or `None`) properties. `resolve_taxon(value)` strips + casefolds against `_TAXON_BY_ALIAS` and returns the matched `Taxon`, raising `ValueError` on unknown — so any alias resolves to the whole record, and callers pull the part they need (`.id`, `.species`, `.common_name`, `.number`). This lets users pass `"human"`, `"9606"`, `"Homo sapiens"`, or `"NCBITaxon:9606"` interchangeably. Ambiguous aliases (`frog`, `xenopus`) are intentionally omitted.

**Naming convention:** `taxon` is an *alias string* a user passes; `taxon_id` is a *resolved canonical* `NCBITaxon:` string (`normalize` resolves its `taxon` arg to `taxon_id` once up front via `resolve_taxon(...).id`, so a bad taxon fails fast); a `Taxon` is the record object.

To append taxon data to a frame, `taxon_mapper(field)` builds a `value -> field` callable for `df[col].map(...)`. `field` is a `TaxonField` (`StrEnum`: `ID` / `NUMBER` / `SPECIES` / `COMMON_NAME`, each value the matching `Taxon` attribute name). The returned callable accepts any alias (so it works on `normalize`'s `Taxon` column, orthology's `Gene2SpeciesTaxonID`, etc.) and yields `None` for unknown or non-string cells — e.g. `df["common_name"] = df["Taxon"].map(taxon_mapper(TaxonField.COMMON_NAME))`.

### `datasets.py` — dataset registry

`AGRDataset` (`StrEnum`: `GENE`, `ORTHOLOGY`, `PHENOTYPES`, `ALLELES`) is the typed handle users pass to `download`/`annotate`. `GENE` is the bulk file backing the gene index — downloaded through the same `download` path as the rest, but built into a `GeneIndex` rather than joined onto a base frame. `DATASETS` maps each member to a `DatasetSpec(bulk, api)`:
- `BulkSpec(data_type, file_type, data_sub_type, join_key)` — a selector into the `/downloads` listing (matched at runtime; never a hardcoded `s3Url`) plus the column its rows join on.
- `ApiSpec(endpoint, join_key, project)` — a per-gene endpoint template and a `project(gene_id, result) -> dict` that flattens one API result into one flat row.

Each dataset has **one natural backend** so output columns stay predictable: orthology → bulk TSV (`ORTHOLOGY-ALLIANCE`, keyed `Gene1ID`; rich columns `Gene2ID`/`Gene2SpeciesTaxonID`/…); phenotypes & alleles → per-gene API (their bulk files are nested per-MOD JSON, deferred). The API orthology projector mirrors the bulk column names so either backend yields the same shape. Adding a dataset = add an enum member + a `DatasetSpec` (+ a projector for API ones).

### `store.py` — atomic Parquet persistence

`write_parquet(df, path)` / `read_parquet(path, *, decode_json=())` back the bulk, per-gene API, and external-annotation caches. Writes go through a same-dir temp file then `os.replace` (atomic), zstd-compressed; object columns holding dicts/lists are JSON-encoded to strings on write (`decode_json` reverses it on read). Ported/trimmed from `src_old/cache.py`. Current projections are flat, so the nested-encoding path is a safety net.

### `annotator.py` — `Annotator` (the user-facing surface)

Ties the lower-level pieces together over a resolved cache dir, with a lazily-built `GeneIndex` cached for the instance's lifetime. Intended use is an **iterative filter-then-requery traversal** — one primary AGR dataset per `annotate` call — so cardinality stays under the caller's control (no cross-dataset Cartesian blow-up).

Cache resolution lives here: module-level `default_cache_dir()` is `$XDG_CACHE_HOME/geneharmony` (falling back to `~/.cache/geneharmony`); `resolve_cache_dir(cache_dir)` returns the user's override or that default, creating it (called once in `__init__`). Pass a path to share a cache between users; omit it for the home default.

The gene index is built lazily in `_gene_index()` and memoized in `self._index` for the instance's lifetime: it calls `download(AGRDataset.GENE)` (the gene bulk file goes to `bulk/gene.parquet` like any other dataset), then builds the index from that parquet in memory (~2 s; **not** pickled — a pickled `GeneIndex` is ~5× the parquet yet saves only ~0.6 s). The parquet is existence-cached and becomes **stale across AGR releases**; `download(AGRDataset.GENE, refresh=True)` (or deleting it) rebuilds.

- `Annotator(cache_dir=None, *, client=None, downloader=None)` — one instance serves any genome; `taxon` is passed per call (`normalize`/`ingest_annotation`/`annotate`), defaulting to `None` (no species filter).
- `async download(dataset, *, refresh=False) -> Path` — resolve the bulk file, stream it, convert TSV→Parquet under `bulk/<dataset>.parquet`, **delete the `.tsv.gz`**; no-op if the parquet exists. `AGRClient`/`Downloader` are created only when a fetch is actually needed (`AsyncExitStack`); passing your own leaves their lifecycle to you. Raises if the dataset has no bulk spec.
- `async normalize(genes, *, taxon=None, limit=1, case_insensitive=False)` — passes through to `GeneIndex.lookup`.
- `async ingest_annotation(source, name, *, gene_id_column, normalize=True, taxon=None, case_insensitive=False, override=False) -> tuple[dict, pd.DataFrame | None]` — store an external table under `external/<name>.parquet`, keyed by canonical `GeneId` (gene ids normalized via the index, replacing the old Rust `normalize_symbols_map`). Ported from `src_old/utils.py`. `gene_id_column` is `str | list[str]`: with a list, the columns are tried **left-to-right per row** and the first identifier that resolves wins (a fallback for tables whose primary ID column has gaps) — normalization is mapping-aware, so a non-null-but-unresolvable value falls through to the next column; `normalize=False` degrades to a plain first-non-null coalesce. The id columns are **kept as-is** and the resolved canonical id is written to a **separate, freshly-added `GeneId` column** (so the input must not already contain a `GeneId` column — that raises); rows where no candidate resolves are dropped and counted. Returns `(summary_dict, unmapped_df)` where `unmapped_df` holds the dropped rows with their original columns (null when the cache hit short-circuits or nothing was dropped).
- `async annotate(genes, *sources, taxon=None, limit=1, case_insensitive=False) -> DataFrame` — build a base frame (`list[str]` → `normalize`, **misses retained** with null cells; or pass a pre-normalized DataFrame), then **wide left-join** each source onto `GeneId` in order. `limit` is forwarded to `normalize`, capping matches per query (`limit=None` = all) — useful for fanning a symbol out to several genes when no `taxon` disambiguates; each matched gene is annotated as its own base row. An `AGRDataset` contributes its **native** columns (bulk filtered by `join_key ∈ gene_ids`, or per-gene API fetched + cached under `api/<dataset>/<id>.parquet`); a `str` names an ingested annotation and contributes `name.`-prefixed columns. An unknown `str` raises.

Per-gene API fetches paginate (`limit`/`page`, page-1-for-total, remaining pages concurrent — both phenotypes and alleles paginate) and are cached one Parquet per gene; cache hits skip the network. Clients are created on demand via `AsyncExitStack` (same pattern as `download`).

### `models.py`

`DownloadFile` (Pydantic) is the only model — it mirrors a `/downloads` entry verbatim (camelCase fields to match the API), with `size: PositiveInt` and `lastModified: datetime`. The former `In_*` / `Out_*` ortholog/phenotype/allele placeholders are gone; the live API projections live as plain-dict `project` callables in `datasets.py` (no Pydantic).

### Placeholder

`main.py` is currently empty — it's the planned user-facing entry point.

## Data flows

**Bulk file:** `AGRClient.list_downloads()` (API host) → pick a `DownloadFile` by `dataType`/`fileType` → `Downloader.download(file.s3Url, dest)` (download host) → `ingest.load_tsv_gz` / `load_json_gz`. Don't hardcode URLs — `s3Url` embeds `releaseVersion`, which changes each release; select by intent and resolve at runtime.

**Gene normalization:** `Annotator._gene_index()` returns a ready `GeneIndex` — `download(AGRDataset.GENE)` yields `bulk/gene.parquet` (downloaded + converted on a cold cache, one-time ~10 s; ~µs lookups) which it builds into the index, memoized on the instance. Then `Annotator.normalize(symbols_or_ids, taxon=..., limit=...)` → `GeneIndex.lookup` → DataFrame of resolved canonical records → proceed with downstream queries using the official `GeneId`. `normalizer.load_gene_index(path)` remains the lower-level "build straight from a TSV path" entry point.

**Annotation (the user-facing flow):** `Annotator(cache_dir)` → optionally `await download(AGRDataset.X)` for bulk datasets → `await annotate(genes, AGRDataset.X, taxon=...)` returns a wide frame on the *normalized* base → slice it (e.g. `df[df.Gene2SpeciesTaxonID == "NCBITaxon:10090"]["Gene2ID"]`) → feed the slice back into the next `annotate` call. One AGR dataset per call; combine with ingested annotations by name in the same call.

## Cache / scratch

Caches default to `~/.cache/geneharmony` (or `$XDG_CACHE_HOME`); the repo-root `cache/` is the manual override used by the notebook and ad-hoc scratch. Layout written by `Annotator`:

```
bulk/<dataset>.parquet            # downloaded + converted bulk datasets (incl. bulk/gene.parquet, the gene-index source)
api/<dataset>/<gene_id>.parquet   # per-gene API results (':' -> '_' in filenames)
external/<name>.parquet           # ingested annotations
```

`agr_http/downloads.json` is a saved snapshot of a `/downloads` listing. `src_old/` is the previous (binary + endpoint-cache) implementation, kept for reference.
