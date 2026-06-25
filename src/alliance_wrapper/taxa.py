"""Species taxon resolution, built from `taxa.json`.

One entry per AGR species. `resolve_taxon` maps any alias — canonical
`NCBITaxon:` ID, bare number, species name or common name — to its `Taxon`
record; `taxon_mapper` builds a `value -> field` callable for annotating a taxon
column of a DataFrame.
"""

import enum
import json
from collections.abc import Callable
from pathlib import Path
from typing import Final, NamedTuple
from importlib.resources import files

_TAXA_PATH = files("alliance_wrapper").joinpath("taxa.json")


class Taxon(NamedTuple):
    """A resolved species: its canonical NCBITaxon ID, species name and common names."""

    id: str
    species: str
    common: tuple[str, ...]

    @property
    def number(self) -> str:
        """The bare NCBI taxon number, without the `NCBITaxon:` prefix."""
        return self.id.split(":", 1)[1]

    @property
    def common_name(self) -> str | None:
        """The primary common name, or None if the species has none."""
        return self.common[0] if self.common else None


class TaxonField(enum.StrEnum):
    ID = "id"
    NUMBER = "number"
    SPECIES = "species"
    COMMON_NAME = "common_name"


def _load_taxa() -> tuple[Taxon, ...]:
    return tuple(
        Taxon(entry["id"], entry["species"], tuple(entry["common"]))
        for entry in json.loads(_TAXA_PATH.read_text())
    )


_TAXA: Final[tuple[Taxon, ...]] = _load_taxa()


_TAXON_BY_ALIAS: Final[dict[str, Taxon]] = {
    alias.casefold(): taxon
    for taxon in _TAXA
    for alias in (taxon.id, taxon.number, taxon.species, *taxon.common)
}


def resolve_taxon(value: str) -> Taxon:
    """Resolve a taxon ID, number, species name or common name to its `Taxon` record.

    Pull out the part you need with `.id`, `.species`, `.common_name` or `.number`.
    """
    taxon = _TAXON_BY_ALIAS.get(value.strip().casefold())
    if taxon is None:
        raise ValueError(f"unknown taxon: {value!r}")
    return taxon


def taxon_mapper(field: TaxonField) -> Callable[[object], str | None]:
    """Build a `value -> field` function for mapping a taxon column.

    The returned callable takes any taxon alias (ID, number, species, common name)
    and returns the requested `field`; unknown or non-string values yield `None`.
    Intended for `df[col].map(taxon_mapper(TaxonField.COMMON_NAME))`.
    """

    def mapper(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        taxon = _TAXON_BY_ALIAS.get(value.strip().casefold())
        return getattr(taxon, field) if taxon is not None else None

    return mapper
