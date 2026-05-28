import pandas as pd

from client import AGRClient
from models import RawOrtholog, Ortholog

def get_orthologs(
    gene_id: str,
    client: AGRClient,
    species: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    params = {}
    if species:
        params["species"] = ",".join(species)
    
    data = client.get(f"/gene/{gene_id}/orthologs", params=params)
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

