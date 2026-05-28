import io
import pandas as pd

from client import AsyncAGRClient

async def get_phenotypes_download(
    gene_id: str,
    client: AsyncAGRClient,
    species: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    params = {}
    if species:
        params["species"] = ",".join(species)

    data = await client.get_text(f"/gene/{gene_id}/phenotypes/download", params=params)

    raw_df = pd.read_csv(io.StringIO(data), sep="\t")
    processed_df = raw_df[["Phenotype", "Genetic Entity ID", "Genetic Entity Name", "Genetic Entity Type"]].drop_duplicates()
    
    return processed_df, raw_df

