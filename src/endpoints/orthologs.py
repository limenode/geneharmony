import pandas as pd

from utils import agr_endpoint
from client import AsyncAGRClient
from models import RawOrtholog, Ortholog

@agr_endpoint("/gene/{gene_id}/orthologs")
async def get_orthologs(
    gene_id: str,
    client: AsyncAGRClient,
) -> tuple[pd.DataFrame, pd.DataFrame]:

    data = await client.get_json(f"/gene/{gene_id}/orthologs")
    results = data["results"] if "results" in data else []

    raw_records = []
    processed_records = []

    for result in results:
        raw_records.append(RawOrtholog(**result).model_dump())

        gtg_orthology = result.get("geneToGeneOrthologyGenerated", {})
        processed_records.append(
            Ortholog(
                subject_id=gtg_orthology.get("subjectGene", "").get("primaryExternalId", ""),
                object_id=gtg_orthology.get("objectGene", "").get("primaryExternalId", ""),
                confidence=gtg_orthology.get("confidence", "").get("name", ""),
            ).model_dump()
        )

    return pd.DataFrame(processed_records), pd.DataFrame(raw_records)

