"""
Async wrapper around the Alliance of Genome Resources (AGR) API.
Allows users to normalize gene identifiers, and annotate gene sets with data from AGR or user-ingested datasets.
"""
__version__ = "0.1.0"

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