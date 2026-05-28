import asyncio
import pandas as pd
from typing import Callable, Coroutine, Any

from client import AsyncAGRClient

async def async_query_gene_list(
    function: Callable[[str, AsyncAGRClient], Coroutine[Any, Any, tuple[pd.DataFrame, pd.DataFrame]]],
    gene_ids: list[str],
    client: AsyncAGRClient,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    results = await asyncio.gather(*[function(gene_id, client) for gene_id in gene_ids])

    processed_dfs, raw_dfs = zip(*results)
    return (
        pd.concat(processed_dfs, ignore_index=True),
        pd.concat(raw_dfs, ignore_index=True),
    )
