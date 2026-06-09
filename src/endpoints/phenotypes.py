import pandas as pd

from endpoints.base import agr_endpoint
from client import AsyncAGRClient
from models import RawPhenotype, Phenotype

@agr_endpoint("/gene/{gene_id}/phenotypes")
async def get_phenotypes(
    gene_id: str,
    client: AsyncAGRClient,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    data = await client.get_json(f"/gene/{gene_id}/phenotypes")
    results = data.get("results", [])

    raw_records = []
    processed_records = []

    for result in results:
        raw_records.append(RawPhenotype(**result).model_dump())
        processed_records.append(
            Phenotype(
                subject_id=result.get("subject", {}).get("primaryExternalId", ""),
                phenotypeStatement=result.get("phenotypeStatement", ""),
            ).model_dump()
        )

    return pd.DataFrame(processed_records), pd.DataFrame(raw_records)

