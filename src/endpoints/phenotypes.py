import pandas as pd

from client import AsyncAGRClient
from models import RawPhenotype, Phenotype

async def get_phenotypes(
    gene_id: str,
    client: AsyncAGRClient,
    species: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    params = {}
    if species:
        params["species"] = ",".join(species)

    data = await client.get_json(f"/gene/{gene_id}/phenotypes", params=params)
    results = data["results"] if "results" in data else []

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

