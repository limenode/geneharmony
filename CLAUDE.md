# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An async Python wrapper around the [Alliance of Genome Resources](https://www.alliancegenome.org) (AGR) REST API. It takes gene symbols, resolves them to gene IDs, fetches orthologs / phenotypes / alleles concurrently, normalizes each response into tidy DataFrames, and caches results on disk. The pipeline is driven interactively from `src/pipeline_nb.ipynb`.

## Environment & commands

Dependencies are managed with [pixi](https://pixi.sh) (conda-forge). There are **no defined tasks, tests, or linters** — `[tasks]` in `pixi.toml` is empty.

```bash
pixi install                                   # create the environment from pixi.lock
pixi run python <script>                       # run Python inside the env
pixi run jupyter lab src/pipeline_nb.ipynb     # open the driver notebook
```

**Imports are flat** (`from client import ...`, `from endpoints import ...`) with no package prefix. Code therefore only resolves when **`src/` is the working directory / on `sys.path`**. Run scripts from inside `src/`; the notebook already runs there.

## Required external setup

- **`gene_normalizer`** — an external binary (not in this repo) that maps gene symbols to IDs. It's resolved by `resolve_gene_normalizer()` in this order: `GENE_NORMALIZER_BIN` env var, then `PATH`.
- **`.env`** at the repo root (see `.env.example`) supplies `GENE_NORMALIZER_BIN`. The notebook loads it via `find_dotenv()` because its cwd is `src/`, not the repo root.

## Architecture

The data flow is: **gene symbols → `gene_normalizer` → gene IDs → endpoint fetch → (processed_df, raw_df) → filesystem cache**.

### The dual-DataFrame contract

Every endpoint and every cache entry deals in a `(processed_df, raw_df)` tuple:
- **processed** — flattened, analysis-ready columns (a Pydantic processed model like `Allele`, `Phenotype`).
- **raw** — the validated full API record (a `Raw*` model), preserving nested dicts/lists.

`models.py` defines both halves per entity. `Raw*` models validate the incoming API shape; the processed models define the flattened output. (Note `RawAllele` fields are deliberately `Optional`/defaulted because the alleles endpoint also returns summary records that omit fields — see commit `d82b54f`.)

### Endpoints (`src/endpoints/`)

Each endpoint is an `async` function decorated with `@agr_endpoint("/gene/{gene_id}/...")` (`base.py`). The decorator attaches a `url_template` attribute to the function; this template is the single source of truth for both the HTTP path **and** the cache key. Adding an endpoint means: write the async function, decorate it with its URL template, return `(processed_df, raw_df)`. `phenotypes_download` is the odd one out — it parses a TSV download rather than JSON.

### Client (`src/client.py`)

`AsyncAGRClient` wraps `httpx.AsyncClient` with an `asyncio.Semaphore` (default `max_concurrent=5`) to bound in-flight requests. Use it as an async context manager, or call `.close()` explicitly (the notebook does). The alleles endpoint paginates (`_PAGE_SIZE=500`): it fetches page 1 for the total count, then `gather`s the remaining pages.

### Cache (`src/cache.py`)

A filesystem cache whose directory tree **mirrors the API path** — `cache/gene/{gene_id}/{endpoint}/`. Each entry holds three files:
- `processed.parquet` (zstd) — columnar, dtype-preserving.
- `raw.pkl.gz` — gzipped pickle, because raw records contain nested object-dtype columns Parquet can't represent natively.
- `meta.json` — `cached_at`, `cache_version`, row counts (the hook for future TTL/invalidation).

Writes go through `_atomic_write` (temp file in the same dir → `os.replace`), and `processed.parquet` is written **last** so its presence — which `has_dataframes` keys off — guarantees the whole entry is complete. Bump `_CACHE_VERSION` when the on-disk shape changes. The cache directory is gitignored.

### Orchestration (`src/utils.py`)

`query_gene_ids(endpoint_fn, cache, gene_ids, client, load_raw=False)` is the entry point that ties it together:
1. Split `gene_ids` into cached vs uncached by checking `cache.has_dataframes(url_template.format(gene_id=...))`.
2. Read cached entries in parallel across CPUs (`ThreadPoolExecutor` driven via `run_in_executor`) — plain file reads release the GIL.
3. Fetch uncached entries concurrently (bounded by the client semaphore), then persist each.
4. `pd.concat` everything into one processed DataFrame (and raw, only when `load_raw=True` — otherwise the pickle read is skipped).
