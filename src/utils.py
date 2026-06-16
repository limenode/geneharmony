import asyncio
import os
import shutil
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from client import AsyncAGRClient
from cache import CacheManager
from endpoints.base import Endpoint

import subprocess


def resolve_gene_normalizer() -> str:
    """Locate the gene_normalizer binary.

    Resolution order:
      1. GENE_NORMALIZER_BIN env var (explicit override, e.g. from .env)
      2. PATH lookup via shutil.which

    Raises FileNotFoundError with an actionable message if it cannot be found.
    """
    override = os.environ.get("GENE_NORMALIZER_BIN")
    if override:
        if not (os.path.isfile(override) and os.access(override, os.X_OK)):
            raise FileNotFoundError(
                f"GENE_NORMALIZER_BIN={override!r} is not an executable file"
            )
        return override

    found = shutil.which("gene_normalizer")
    if found is None:
        raise FileNotFoundError(
            "gene_normalizer not found. Set GENE_NORMALIZER_BIN in your .env "
            "(see .env.example) or put gene_normalizer on PATH."
        )
    return found

def normalize_symbols(symbols: list[str], species: str = "") -> list[str]:
    joined_symbols = "\n".join(symbols)
    
    command = [resolve_gene_normalizer(), "--no-echo", "--id-only"]
    if species:
        command.extend(["--species", species])
    
    try:
        result = subprocess.run(
            command,
            input=joined_symbols,
            text=True,
            capture_output=True,
            check=True,
        )
        normalized = result.stdout.strip().splitlines()
        normalized = list(filter(None, (s.strip() for s in normalized)))
        return normalized
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"gene_normalizer failed with exit code {e.returncode}: {e.stderr}"
        ) from e

def normalize_symbols_map(symbols: list[str], species: str = "") -> dict[str, str]:
    """Map each input symbol to its normalized gene ID.

    The binary emits one line per input line (blank when a symbol can't be resolved),
    so the output stays positionally 1:1 with the input.

    Raises RuntimeError if the binary exits non-zero, or if the line count does
    not match the input (i.e. the 1:1 assumption is violated, e.g. a symbol
    resolving to multiple IDs) — failing loudly beats silently misaligning rows.
    """
    joined_symbols = "\n".join(symbols)

    command = [resolve_gene_normalizer(), "--no-echo", "--id-only"]
    if species:
        command.extend(["--species", species])

    try:
        result = subprocess.run(
            command,
            input=joined_symbols,
            text=True,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"gene_normalizer failed with exit code {e.returncode}: {e.stderr}"
        ) from e

    lines = result.stdout.splitlines()
    if len(lines) != len(symbols):
        raise RuntimeError(
            f"gene_normalizer returned {len(lines)} lines for {len(symbols)} "
            "input symbols; cannot align symbol -> id mapping by position."
        )

    return {sym: gid.strip() for sym, gid in zip(symbols, lines) if gid.strip()}

async def query_gene_ids(
    function: Endpoint,
    cache: CacheManager,
    gene_ids: list[str],
    client: AsyncAGRClient,
    load_raw: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    url_template: str = function.url_template

    cached_ids   = [gid for gid in gene_ids if cache.has_dataframes(url_template.format(gene_id=gid))]
    uncached_ids = [gid for gid in gene_ids if gid not in set(cached_ids)]

    all_results: list[tuple[pd.DataFrame, pd.DataFrame]] = []

    # Step 2a — load cached genes
    if cached_ids:
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
            cached_results = await asyncio.gather(*[
                loop.run_in_executor(
                    executor,
                    lambda gid=gid: cache.get_dataframes(url_template.format(gene_id=gid), load_raw=load_raw),
                )
                for gid in cached_ids
            ])
        all_results.extend(cached_results)

    for gid in uncached_ids:
        print(f"Gene ID {gid} not found in cache. Fetching from API...")

    # Step 2b — fetch uncached genes: async HTTP requests, bounded by the client
    # semaphore. Each gene's fetch and its cache write are bundled into one
    # coroutine so a finished gene is persisted immediately, instead of waiting
    # at a gather() barrier for the slowest request before any write starts.
    # set_dataframes is blocking disk I/O, so it's offloaded to a thread to keep
    # it from stalling the event loop (and the other in-flight fetches).
    if uncached_ids:
        loop = asyncio.get_running_loop()

        async def _fetch_and_store(gid: str) -> tuple[pd.DataFrame, pd.DataFrame]:
            processed_df, raw_df = await function(gid, client)
            await loop.run_in_executor(
                None,
                cache.set_dataframes,
                url_template.format(gene_id=gid),
                processed_df,
                raw_df,
            )
            return processed_df, raw_df

        fetched_results = await asyncio.gather(*[
            _fetch_and_store(gid) for gid in uncached_ids
        ])
        all_results.extend(fetched_results)

    processed_dfs, raw_dfs = zip(*all_results)
    return (
        pd.concat(processed_dfs, ignore_index=True),
        pd.concat(raw_dfs, ignore_index=True) if load_raw else pd.DataFrame(),
    )


# Canonical gene-ID column name used inside every stored annotation set.
_ANNOTATION_GENE_ID = "gene_id"

def _read_annotation_table(path: str | Path) -> pd.DataFrame:
    """Read a flat annotation file, dispatching on its extension."""
    suffix = Path(path).suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(path)
    if suffix in (".tsv", ".tab", ".txt"):
        return pd.read_csv(path, sep="\t")
    if suffix == ".csv":
        return pd.read_csv(path)
    raise ValueError(
        f"Unsupported annotation file type {suffix!r} for {path!r} "
        "(expected .csv, .tsv/.tab/.txt, or .parquet)."
    )


def ingest_annotation(
    cache: CacheManager,
    source: str | Path | pd.DataFrame,
    annotation_name: str,
    gene_id_column: str,
    species: str = "",
    normalize: bool = True,
    override: bool = False,
) -> dict:
    """Injest a gene annotation table into the cache, keyed by ``annotation_name``.

    Args:
        cache (CacheManager): cache to store the annotation DataFrame in_
        source (str | Path | pd.DataFrame): either a path to a flat file (CSV, TSV, or Parquet) or a DataFrame in memory
        annotation_name (str): the name to store the annotation under in the cache; used for later retrieval and joining
        gene_id_column (str): the column name containing the gene IDs in the source data
        species (str, optional): the species for which to normalize gene IDs. Defaults to "".
        normalize (bool, optional): whether to normalize gene IDs. Defaults to True.

    Raises:
        KeyError: if the specified gene ID column is not found in the source data
        ValueError: if there is a collision with an existing gene ID column

    Returns:
        dict: a summary of the ingestion process (rows in/stored/dropped, columns)
    """
    
    if cache.has_annotation(annotation_name) and not override:
        print(f"Annotation {annotation_name!r} already exists in cache; skipping ingestion.")
        return {
            "annotation_name": annotation_name,
            "rows_in": None,
            "rows_stored": None,
            "rows_dropped_unmapped": None,
            "columns": None,
            "normalized": None,
        }
    
    df = source.copy() if isinstance(source, pd.DataFrame) else _read_annotation_table(source)

    if gene_id_column not in df.columns:
        raise KeyError(
            f"gene_id_column {gene_id_column!r} not found; columns are {list(df.columns)}"
        )
    if gene_id_column != _ANNOTATION_GENE_ID and _ANNOTATION_GENE_ID in df.columns:
        raise ValueError(
            f"input already has a {_ANNOTATION_GENE_ID!r} column distinct from "
            f"{gene_id_column!r}; rename it to avoid a collision."
        )

    df = df.rename(columns={gene_id_column: _ANNOTATION_GENE_ID})

    rows_in = len(df)
    rows_dropped = 0
    if normalize:
        symbols = df[_ANNOTATION_GENE_ID].astype(str).tolist()
        # Map only the unique symbols, then broadcast back across all rows.
        unique_symbols = list(dict.fromkeys(symbols))
        mapping = normalize_symbols_map(unique_symbols, species=species)
        df[_ANNOTATION_GENE_ID] = df[_ANNOTATION_GENE_ID].astype(str).map(mapping)
        unmapped = df[_ANNOTATION_GENE_ID].isna()
        rows_dropped = int(unmapped.sum())
        df = df[~unmapped].reset_index(drop=True)

    cache.set_annotation(annotation_name, df)

    return {
        "annotation_name": annotation_name,
        "rows_in": rows_in,
        "rows_stored": len(df),
        "rows_dropped_unmapped": rows_dropped,
        "columns": [c for c in df.columns if c != _ANNOTATION_GENE_ID],
        "normalized": normalize,
    }


def query_annotations(
    cache: CacheManager,
    annotation_name: str,
    gene_ids: list[str],
) -> pd.DataFrame:
    """Return the rows of an annotation set whose gene_id is in ``gene_ids``."""
    df = cache.get_annotation(annotation_name)
    return df[df[_ANNOTATION_GENE_ID].isin(set(gene_ids))].reset_index(drop=True)


def annotate(
    agr_df: pd.DataFrame,
    cache: CacheManager,
    annotation_names: str | list[str],
    gene_id_col: str = _ANNOTATION_GENE_ID,
) -> pd.DataFrame:
    """Left-join one or more annotation sets onto an AGR processed DataFrame.

    Joins on ``agr_df[gene_id_col]`` against each set's ``gene_id``.
    """
    if isinstance(annotation_names, str):
        annotation_names = [annotation_names]
    if gene_id_col not in agr_df.columns:
        raise KeyError(
            f"gene_id_col {gene_id_col!r} not found; columns are {list(agr_df.columns)}"
        )

    out = agr_df.copy()
    for name in annotation_names:
        ann = cache.get_annotation(name)
        ann = ann.rename(
            columns={c: f"{name}.{c}" for c in ann.columns if c != _ANNOTATION_GENE_ID}
        )
        out = out.merge(
            ann, how="left", left_on=gene_id_col, right_on=_ANNOTATION_GENE_ID
        )
        # When joining on a differently-named column, drop the duplicate key the
        # merge pulled in from the annotation side.
        if gene_id_col != _ANNOTATION_GENE_ID:
            out = out.drop(columns=[_ANNOTATION_GENE_ID])
    return out