"""In-memory gene normalizer built from the GENE-TSV-COMBINED bulk file.

`load_gene_index` reads the file and precomputes O(1) lookups from every
identifier form — primary ID, deprecated (secondary) ID, official symbol,
synonym and systematic name — to row positions in the loaded table.

`GeneIndex.normalize` takes one query or a list and returns a DataFrame with one
row per match: the original `query`, the `match_kind`, and every column of the
matched gene record. Matches are ranked by precedence (primary ID > secondary ID
> official symbol > synonym); `limit` caps matches per query and `taxon` narrows
symbols that recur across species. Unmatched queries are still returned, with a
null `match_kind`. Matching is case-sensitive unless `case_insensitive=True`,
since case can be meaningful across species (human TP53 vs mouse Trp53).
"""

import enum
import json
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import pandas as pd

from ingest import load_tsv_gz

type _Tables = dict["MatchKind", dict[str, list[int]]]

_TAXA_PATH = Path(__file__).parent / "taxa.json"


def _load_taxon_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for entry in json.loads(_TAXA_PATH.read_text()):
        taxon_id: str = entry["id"]
        number = taxon_id.split(":", 1)[1]
        for alias in (taxon_id, number, entry["species"], *entry["common"]):
            lookup[alias.casefold()] = taxon_id
    return lookup


_TAXON_LOOKUP: dict[str, str] = _load_taxon_lookup()


def resolve_taxon(value: str) -> str:
    """Resolve a taxon ID, number, species name or common name to its NCBITaxon ID."""
    taxon_id = _TAXON_LOOKUP.get(value.strip().casefold())
    if taxon_id is None:
        raise ValueError(f"unknown taxon: {value!r}")
    return taxon_id


class MatchKind(enum.IntEnum):
    PRIMARY_ID = 0
    SECONDARY_ID = 1
    OFFICIAL_SYMBOL = 2
    SYNONYM = 3


class GeneMatch(NamedTuple):
    row: int
    kind: MatchKind


@dataclass(slots=True)
class GeneIndex:
    records: pd.DataFrame
    _taxa: tuple[str, ...]
    _exact: _Tables
    _folded: _Tables | None = None

    def normalize(
        self,
        queries: str | list[str],
        *,
        taxon: str | None = None,
        limit: int | None = 1,
        case_insensitive: bool = False,
    ) -> pd.DataFrame:
        if isinstance(queries, str):
            queries = [queries]
        if taxon is not None:
            taxon = resolve_taxon(taxon)

        order: list[int] = []
        query_col: list[str] = []
        kind_col: list[str] = []
        rows: list[int] = []
        miss_order: list[int] = []
        miss_query: list[str] = []

        for i, query in enumerate(queries):
            matches = self._resolve(query, taxon, case_insensitive)
            if limit is not None:
                matches = matches[:limit]
            if matches:
                for match in matches:
                    order.append(i)
                    query_col.append(query)
                    kind_col.append(match.kind.name)
                    rows.append(match.row)
            else:
                miss_order.append(i)
                miss_query.append(query)

        matched = self.records.iloc[rows].reset_index(drop=True)
        matched.insert(0, "match_kind", kind_col)
        matched.insert(0, "query", query_col)
        matched.insert(0, "_order", order)

        if miss_query:
            missed = pd.DataFrame({"_order": miss_order, "query": miss_query, "match_kind": None})
            matched = pd.concat([matched, missed], ignore_index=True)

        return (
            matched.sort_values("_order", kind="stable")
            .drop(columns="_order")
            .reset_index(drop=True)
        )

    def _resolve(self, query: str, taxon: str | None, case_insensitive: bool) -> list[GeneMatch]:
        tables = self._exact
        key = query
        if case_insensitive:
            tables = self._folded_tables()
            key = query.casefold()

        matches = [
            GeneMatch(row, kind)
            for kind in MatchKind
            for row in tables[kind].get(key, ())
        ]
        if taxon is not None:
            matches = [m for m in matches if self._taxa[m.row] == taxon]

        seen: set[int] = set()
        unique: list[GeneMatch] = []
        for match in matches:
            if match.row not in seen:
                seen.add(match.row)
                unique.append(match)
        return unique

    def _folded_tables(self) -> _Tables:
        if self._folded is None:
            self._folded = _fold(self._exact)
        return self._folded


def _fold(tables: _Tables) -> _Tables:
    folded: _Tables = {}
    for kind, table in tables.items():
        merged: dict[str, list[int]] = {}
        for key, table_rows in table.items():
            merged.setdefault(key.casefold(), []).extend(table_rows)
        folded[kind] = merged
    return folded


def build_gene_index(records: pd.DataFrame) -> GeneIndex:
    primary: dict[str, list[int]] = {}
    secondary: dict[str, list[int]] = {}
    official: dict[str, list[int]] = {}
    synonym: dict[str, list[int]] = {}

    for i, gene_id in enumerate(records["GeneId"].tolist()):
        primary.setdefault(gene_id, []).append(i)

    for i, symbol in enumerate(records["GeneSymbol"].tolist()):
        if isinstance(symbol, str):
            official.setdefault(symbol, []).append(i)

    for column in ("GeneSynonyms", "GeneSystematicName"):
        for i, value in enumerate(records[column].tolist()):
            if isinstance(value, str):
                for token in value.split("|"):
                    synonym.setdefault(token, []).append(i)

    for i, value in enumerate(records["GeneSecondaryIds"].tolist()):
        if isinstance(value, str):
            for token in value.split("|"):
                secondary.setdefault(token, []).append(i)

    exact: _Tables = {
        MatchKind.PRIMARY_ID: primary,
        MatchKind.SECONDARY_ID: secondary,
        MatchKind.OFFICIAL_SYMBOL: official,
        MatchKind.SYNONYM: synonym,
    }
    return GeneIndex(records, tuple(records["Taxon"].tolist()), exact)


def load_gene_index(path: Path) -> GeneIndex:
    return build_gene_index(load_tsv_gz(path, dtype=str))
