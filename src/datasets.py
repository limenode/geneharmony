"""Registry of AGR datasets the annotator can pull, with their backends.

Each `AGRDataset` maps to a `DatasetSpec` describing how to obtain it:
- `bulk` — a selector into the `/downloads` listing (matched at runtime, never a
  hardcoded `s3Url`) plus the column its rows join on.
- `api` — a per-gene endpoint template, the column its projected rows join on,
  and a `project` callable flattening one API result into a single flat row.

Orthology is served from its bulk TSV (complete, richly columned); phenotypes and
alleles from the per-gene API (their bulk files are nested per-MOD JSON, deferred).
The API orthology projection mirrors the bulk column names so either backend
yields the same `Gene1ID`/`Gene2ID`/`Gene2SpeciesTaxonID` shape. `GENE` is the
bulk file backing the in-memory gene index — downloaded through the same path,
but built into a `GeneIndex` rather than joined onto a base frame.
"""

import enum
from collections.abc import Callable
from typing import Any, Final, NamedTuple

type Json = dict[str, Any]
type Projector = Callable[[str, Json], dict[str, Any]]


class AGRDataset(enum.StrEnum):
    GENE = "gene"
    ORTHOLOGY = "orthology"
    PHENOTYPES = "phenotypes"
    ALLELES = "alleles"


class BulkSpec(NamedTuple):
    data_type: str
    file_type: str
    data_sub_type: str
    join_key: str


class ApiSpec(NamedTuple):
    endpoint: str
    join_key: str
    project: Projector


class DatasetSpec(NamedTuple):
    bulk: BulkSpec | None
    api: ApiSpec | None


def _project_orthologs(gene_id: str, result: Json) -> dict[str, Any]:
    g = result.get("geneToGeneOrthologyGenerated", {})
    subject = g.get("subjectGene", {})
    obj = g.get("objectGene", {})
    return {
        "Gene1ID": subject.get("primaryExternalId", gene_id),
        "Gene1Symbol": subject.get("geneSymbol", {}).get("displayText"),
        "Gene1SpeciesTaxonID": subject.get("taxon", {}).get("curie"),
        "Gene2ID": obj.get("primaryExternalId"),
        "Gene2Symbol": obj.get("geneSymbol", {}).get("displayText"),
        "Gene2SpeciesTaxonID": obj.get("taxon", {}).get("curie"),
        "Gene2SpeciesName": obj.get("taxon", {}).get("name"),
        "Confidence": g.get("confidence", {}).get("name"),
        "IsBestScore": g.get("isBestScore", {}).get("name"),
        "IsBestRevScore": g.get("isBestScoreReverse", {}).get("name"),
    }


def _project_phenotypes(gene_id: str, result: Json) -> dict[str, Any]:
    return {
        "gene_id": gene_id,
        "phenotypeStatement": result.get("phenotypeStatement"),
        "references": "|".join(result.get("pubmedPubModIDs") or []),
    }


def _project_alleles(gene_id: str, result: Json) -> dict[str, Any]:
    variants = result.get("variantList") or []
    variant = variants[0] if variants else {}
    return {
        "gene_id": gene_id,
        "allele_id": (result.get("allele") or {}).get("curie"),
        "symbol": result.get("symbol"),
        "alterationType": result.get("alterationType"),
        "hasPhenotype": result.get("hasPhenotype", False),
        "hasDisease": result.get("hasDisease", False),
        "variantType": (variant.get("variantType") or {}).get("name"),
    }


DATASETS: Final[dict[AGRDataset, DatasetSpec]] = {
    AGRDataset.GENE: DatasetSpec(
        bulk=BulkSpec("GENE", "TSV", "COMBINED", "GeneId"),
        api=None,
    ),
    AGRDataset.ORTHOLOGY: DatasetSpec(
        bulk=BulkSpec("ORTHOLOGY-ALLIANCE", "TSV", "COMBINED", "Gene1ID"),
        api=ApiSpec("/gene/{gene_id}/orthologs", "Gene1ID", _project_orthologs),
    ),
    AGRDataset.PHENOTYPES: DatasetSpec(
        bulk=None,
        api=ApiSpec("/gene/{gene_id}/phenotypes", "gene_id", _project_phenotypes),
    ),
    AGRDataset.ALLELES: DatasetSpec(
        bulk=None,
        api=ApiSpec("/gene/{gene_id}/alleles", "gene_id", _project_alleles),
    ),
}
