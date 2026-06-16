import io
import pandas as pd

from endpoints.base import agr_endpoint
from client import AsyncAGRClient

@agr_endpoint("/gene/{gene_id}/phenotypes/download")
async def get_phenotypes_download(
    gene_id: str,
    client: AsyncAGRClient,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    data = await client.get_data(f"/gene/{gene_id}/phenotypes/download")

    raw_df = pd.read_csv(io.StringIO(data), sep="\t")
    processed_df = raw_df[["Phenotype", "Genetic Entity ID", "Genetic Entity Name", "Genetic Entity Type"]].drop_duplicates()
    # Stamp the queried gene so this output is joinable like every other endpoint.
    processed_df = processed_df.assign(gene_id=gene_id)

    return processed_df, raw_df

