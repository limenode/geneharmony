# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An async Python wrapper around the [Alliance of Genome Resources](https://www.alliancegenome.org) (AGR) REST API and its bulk-download files. It resolves gene symbols/IDs to canonical genes with an in-memory index built from AGR's `GENE-TSV-COMBINED` bulk file, fetches API data concurrently, and downloads/parses bulk files. The pipeline is developed interactively in `src/notebook.ipynb`; `src/main.py` is the intended user-facing entry point.

This replaces an earlier design (kept in `src_old/` for reference) that used an external Rust `gene_normalizer` binary and a filesystem cache of per-endpoint DataFrames. Gene normalization is now pure Python against the bulk file — there is **no external binary and no `.env`** to set up (`.env.example` is stale).

## Environment & commands

Dependencies are managed with [pixi](https://pixi.sh) (conda-forge). There are **no defined tasks, tests, or linters** — `[tasks]` in `pixi.toml` is empty. Python is 3.14; deps include `httpx`, `pydantic` v2, `pandas` 3.x, `pyarrow`.

```bash
pixi install                                # create the environment from pixi.lock
pixi run python <script>                    # run Python inside the env
pixi run jupyter lab src/notebook.ipynb     # open the driver notebook
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

`build_gene_index` fills four `dict[str, list[int]]` tables mapping each identifier form to row positions in the retained `records` DataFrame, tagged by `MatchKind`:

```
PRIMARY_ID  >  SECONDARY_ID  >  OFFICIAL_SYMBOL  >  SYNONYM      (precedence, high→low)
```

**Precedence is the `MatchKind` enum's definition order**, surfaced by `for kind in MatchKind` in `_resolve` (there is no explicit sort; the int values are documentation only). Keep members declared best-to-worst.

`GeneIndex.normalize(queries, *, taxon=None, limit=1, case_insensitive=False) -> pd.DataFrame`:
- `queries`: `str | list[str]` (scalar is wrapped). Built for batches of thousands.
- Returns one row per `(query, match)`: columns `query`, `match_kind`, then **all** gene-record columns. Input order preserved.
- **Unmatched queries are retained** with null `match_kind` (and `NaN` record cells) so misses are visible. Filter with `df.match_kind.notna()`.
- `limit` caps matches per query (`limit=None` = all → useful for top-N when no taxon disambiguates). Pipeline is **precedence-sort → taxon-filter → dedup → limit** (i.e. filter-then-limit). A kind filter, if ever wanted, is left to the caller on the returned frame — note that user-side kind-filtering happens *after* `limit`.
- Within a tier, matches are ordered by **row/file order** (not species priority).
- `case_insensitive=True` consults a **lazily-built** casefolded copy of the tables (zero cost otherwise). Default is case-sensitive, since case can be meaningful across species (human `TP53` vs mouse `Trp53`).

### Taxon resolution — `taxa.json` + `resolve_taxon`

`taxa.json` (in `src/`, read via a path relative to `normalizer.py`) holds one entry per species with `id` / `species` / `common`. At import, `_load_taxon_lookup` flattens every alias — full `NCBITaxon:` ID, bare number, species name, and each common name — into one casefolded `dict[str, str]`. `resolve_taxon(value)` strips + casefolds and returns the canonical `NCBITaxon:` ID, raising `ValueError` on unknown. `normalize` resolves `taxon` once up front, so a bad taxon fails fast. This lets users pass `"human"`, `"9606"`, `"Homo sapiens"`, or `"NCBITaxon:9606"` interchangeably. Ambiguous aliases (`frog`, `xenopus`) are intentionally omitted.

### `models.py`

`DownloadFile` (Pydantic) is the **active** model — it mirrors a `/downloads` entry verbatim (camelCase fields to match the API), with `size: PositiveInt` and `lastModified: datetime`. The `In_*` / `Out_*` ortholog/phenotype/allele models are **legacy placeholders** carried over from `src_old/`; they predate the current API inspection and need reworking before use (the live `/gene/{id}` response nests under a `gene` key with different field names).

### Placeholder

`main.py` is currently empty — it's the planned user-facing entry point.

## Data flows

**Bulk file:** `AGRClient.list_downloads()` (API host) → pick a `DownloadFile` by `dataType`/`fileType` → `Downloader.download(file.s3Url, dest)` (download host) → `ingest.load_tsv_gz` / `load_json_gz`. Don't hardcode URLs — `s3Url` embeds `releaseVersion`, which changes each release; select by intent and resolve at runtime.

**Gene normalization (preprocessing):** download `GENE-TSV-COMBINED` → `load_gene_index` (one-time ~10 s build; ~µs lookups) → `normalize(symbols_or_ids, taxon=..., limit=...)` → DataFrame of resolved canonical records → proceed with downstream queries using the official `GeneId`.

## Cache / scratch

Downloaded bulk files and other artifacts live under `cache/` at the repo root. `agr_http/downloads.json` is a saved snapshot of a `/downloads` listing. `src_old/` is the previous (binary + endpoint-cache) implementation, kept for reference.
