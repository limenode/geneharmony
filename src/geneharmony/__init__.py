"""
geneharmony — toolkit to normalize gene identifiers and annotate gene sets.
Resolves symbols/IDs against the Alliance of Genome Resources (AGR) and annotates with data from AGR or user-ingested datasets.
"""
from ._version import __version__
from .annotator import Annotator
from .datasets import AGRDataset
from .taxa import TaxonField, resolve_taxon, taxon_mapper

__all__ = [
    "AGRDataset",
    "Annotator",
    "TaxonField",
    "__version__",
    "resolve_taxon",
    "taxon_mapper",
]
