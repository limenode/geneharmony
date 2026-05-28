import asyncio
import os
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Coroutine, Any

from client import AsyncAGRClient
from cache import CacheManager

def agr_endpoint(url: str):
    """Attach a URL template to an endpoint function.

    Usage:
        @agr_endpoint("/gene/{gene_id}/alleles")
        async def get_alleles(gene_id, client): ...

        get_alleles.__url__                               # "/gene/{gene_id}/alleles"
        get_alleles.__url__.format(gene_id="HGNC:1100")  # "/gene/HGNC:1100/alleles"
    """
    def decorator(func):
        func.__url__ = url
        return func
    return decorator

async def query_gene_ids(
    function: Callable[[str, AsyncAGRClient], Coroutine[Any, Any, tuple[pd.DataFrame, pd.DataFrame]]],
    cache: CacheManager,
    gene_ids: list[str],
    client: AsyncAGRClient,
    load_raw: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    url_template: str = function.__url__

    cached_ids   = [gid for gid in gene_ids if cache.has_dataframes(url_template.format(gene_id=gid))]
    uncached_ids = [gid for gid in gene_ids if gid not in set(cached_ids)]

    all_results: list[tuple[pd.DataFrame, pd.DataFrame]] = []

    # Step 2a — load cached genes: plain file reads, parallelised across all cores.
    # The lambda captures gid by default argument to avoid the loop closure pitfall,
    # and forwards load_raw so the pickle read can be skipped when not needed.
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
