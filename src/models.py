from datetime import datetime
from pydantic import BaseModel, PositiveInt
from typing import Optional, List

class DownloadFile(BaseModel):
    filename: str
    s3Path: str
    s3Url: str
    stableURL: str
    releaseVersion: str
    size: PositiveInt
    lastModified: datetime
    dataType: str
    fileType: str
    dataSubType: str
    fileExtension: str

class In_Ortholog(BaseModel):
    category: str
    searchable: bool
    stringencyFilter: str
    geneAnnotations: List[dict]
    geneAnnotationsMap: dict
    geneToGeneOrthologyGenerated: dict

class Out_Ortholog(BaseModel):
    gene_id: str
    object_id: str
    confidence: str

class In_Phenotype(BaseModel):
    subject: dict
    relation: dict
    primaryAnnotations: List[dict]
    category: str
    searchable: bool
    uniqueId: str
    phenotypeStatement: str
    references: List[dict]
    pubmedPubModIDs: List[str]

class Out_Phenotype(BaseModel):
    gene_id: str
    phenotypeStatement: str

class In_Allele(BaseModel):
    category: Optional[str] = None
    searchable: bool = False
    allele: dict = {}
    geneIds: List[str] = []
    symbol: Optional[str] = None
    alterationType: Optional[str] = None
    alterationTypeSortOrder: Optional[int] = None
    hasPhenotype: bool = False
    hasDisease: bool = False
    variantList: List[dict] = []

class Out_Allele(BaseModel):
    gene_id: str
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
