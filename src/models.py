from pydantic import BaseModel, PositiveInt
from typing import Optional, List

class RawOrtholog(BaseModel):
    category: str
    searchable: bool
    stringencyFilter: str
    geneAnnotations: List[dict]
    geneAnnotationsMap: dict
    geneToGeneOrthologyGenerated: dict

class Ortholog(BaseModel):
    subject_id: str
    object_id: str
    confidence: str

class RawPhenotype(BaseModel):
    subject: dict
    relation: dict
    primaryAnnotations: List[dict]
    category: str
    searchable: bool
    uniqueId: str
    phenotypeStatement: str
    references: List[dict]
    pubmedPubModIDs: List[str]

class Phenotype(BaseModel):
    subject_id: str
    phenotypeStatement: str

class RawAllele(BaseModel):
    category: str
    searchable: bool
    allele: dict
    geneIds: List[str]
    symbol: Optional[str] = None
    alterationType: str
    alterationTypeSortOrder: int
    hasPhenotype: bool
    hasDisease: bool
    variantList: List[dict]

class Allele(BaseModel):
    allele_id: str
    symbol: Optional[str] = None
    alteration_type: str
    has_phenotype: bool
    has_disease: bool
    variant_type: Optional[str] = None
    chromosome: Optional[str] = None
    assembly: Optional[str] = None
    start: Optional[int] = None
    end: Optional[int] = None
    ref: Optional[str] = None
    alt: Optional[str] = None
    hgvs_g: Optional[str] = None
    hgvs_c: Optional[str] = None
    most_severe_consequence: Optional[str] = None
    rs_id: Optional[str] = None
