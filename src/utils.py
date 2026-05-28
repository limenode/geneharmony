from client import AGRClient

from typing import Callable
import pandas as pd

def query_gene_list(
    function: Callable[[str, AGRClient], tuple[pd.DataFrame, pd.DataFrame]],
    gene_ids: list[str],
    client: AGRClient,
) -> pd.DataFrame:
    results = []
    raw_results = []
    for gene_id in gene_ids:
        processed_df, raw_df = function(gene_id, client)
        results.append(processed_df)
        raw_results.append(raw_df)
    return pd.concat(results, ignore_index=True), pd.concat(raw_results, ignore_index=True)
