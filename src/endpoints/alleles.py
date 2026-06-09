import asyncio
import math
import pandas as pd

from endpoints.base import agr_endpoint
from client import AsyncAGRClient
from models import RawAllele, Allele

_PAGE_SIZE = 500

@agr_endpoint("/gene/{gene_id}/alleles")
async def get_alleles(
    gene_id: str,
    client: AsyncAGRClient,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    # Fetch page 1 first for total count
    first_page = await client.get_json(
        f"/gene/{gene_id}/alleles",
        params={"limit": _PAGE_SIZE, "page": 1},
    )
    total = first_page.get("total", 0)
    num_pages = math.ceil(total / _PAGE_SIZE)

    if num_pages > 1:
        remaining = await asyncio.gather(*[
            client.get_json(
                f"/gene/{gene_id}/alleles",
                params={"limit": _PAGE_SIZE, "page": p},
            )
            for p in range(2, num_pages + 1)
        ])
    else:
        remaining = []

    raw_records = []
    processed_records = []
    for page_data in [first_page, *remaining]:
        for result in page_data.get("results", []):
            raw_records.append(RawAllele(**result).model_dump())
            processed_records.append(_process_allele(result).model_dump())

    return pd.DataFrame(processed_records), pd.DataFrame(raw_records)


def _process_allele(result: dict) -> Allele:
    variant_list = result.get("variantList") or []
    variant = variant_list[0] if variant_list else {}

    locations = variant.get("curatedVariantGenomicLocations") or []
    loc = locations[0] if locations else {}
    loc_obj = loc.get("variantGenomicLocationAssociationObject") or {}
    assembly = (loc_obj.get("genomeAssembly") or {}).get("primaryExternalId")

    cross_refs = variant.get("crossReferences") or []
    rs_id = next(
        (r["referencedCurie"] for r in cross_refs if r.get("referencedCurie", "").startswith("rs")),
        None,
    )

    return Allele(
        allele_id=result.get("allele", {}).get("curie", ""),
        symbol=result.get("symbol", ""),
        alteration_type=result.get("alterationType", ""),
        has_phenotype=result.get("hasPhenotype", False),
        has_disease=result.get("hasDisease", False),
        variant_type=(variant.get("variantType") or {}).get("name"),
        chromosome=loc_obj.get("name"),
        assembly=assembly,
        start=loc.get("start"),
        end=loc.get("end"),
        ref=loc.get("referenceSequence"),
        alt=loc.get("variantSequence"),
        hgvs_g=loc.get("hgvs"),
        hgvs_c=(loc.get("hgvsC") or [None])[0],
        most_severe_consequence=((loc.get("mostSevereConsequence") or {}).get("vepImpact") or {}).get("name"),
        rs_id=rs_id,
    )
