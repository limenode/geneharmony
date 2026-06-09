import asyncio
import os
import shutil
import pandas as pd
from concurrent.futures import ThreadPoolExecutor

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

    # Step 2b — fetch uncached genes: async HTTP requests, bounded by the client semaphore.
    if uncached_ids:
        fetched_results = await asyncio.gather(*[
            function(gid, client) for gid in uncached_ids
        ])
        # Step 3 — persist each result so future calls hit the cache.
        for gid, (processed_df, raw_df) in zip(uncached_ids, fetched_results):
            cache.set_dataframes(url_template.format(gene_id=gid), processed_df, raw_df)
        all_results.extend(fetched_results)

    processed_dfs, raw_dfs = zip(*all_results)
    return (
        pd.concat(processed_dfs, ignore_index=True),
        pd.concat(raw_dfs, ignore_index=True) if load_raw else pd.DataFrame(),
    )
