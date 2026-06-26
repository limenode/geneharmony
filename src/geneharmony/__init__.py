"""
geneharmony — async toolkit to normalize gene identifiers and annotate gene sets.
Resolves symbols/IDs against the Alliance of Genome Resources (AGR) and annotates with data from AGR or user-ingested datasets.
"""
__version__ = "0.3.0"

from .annotator import Annotator
from .datasets import AGRDataset
from .taxa import TaxonField, taxon_mapper, resolve_taxon

__all__ = [
    "Annotator",
    "AGRDataset",
    "TaxonField",
    "taxon_mapper",
    "resolve_taxon",
]