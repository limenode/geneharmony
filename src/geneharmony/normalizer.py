"""In-memory gene normalizer built from the GENE-TSV-COMBINED bulk file.

`load_gene_index` reads the file and precomputes O(1) lookups from every
identifier form — primary ID, deprecated (secondary) ID, official symbol,
synonym, systematic name and external cross-reference (e.g. `NCBI_Gene:`,
`ENSEMBL:`, `UniProtKB:`) — to row positions in the loaded table.

`GeneIndex.lookup` takes one query or a list and returns a DataFrame with one
row per match: the original `query`, the `match_kind`, and every column of the
matched gene record. Matches are ranked by precedence (primary ID > secondary ID
> official symbol > synonym > cross-reference); `limit` caps matches per query and `taxon` narrows
symbols that recur across species. Unmatched queries are still returned, with a
null `match_kind`. Matching is case-sensitive unless `case_insensitive=True`,
since case can be meaningful across species (human TP53 vs mouse Trp53).
"""

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NamedTuple
import pandas as pd

from .ingest import load_tsv_gz
from .taxa import resolve_taxon

type _Tables = dict["MatchKind", dict[str, list[int]]]

# Cross-reference databases whose IDs denote protein families / enzyme classes
# rather than genes; one such token fans out to hundreds of genes, so they are
# excluded from the index. Keys are the token prefix before the first ':'.
_XREF_EXCLUDED_PREFIXES: Final[frozenset[str]] = frozenset(
    {"PANTHER", "TreeFam", "ExPASy", "TCDB"}
)


class MatchKind(enum.IntEnum):
    PRIMARY_ID = 0
    SECONDARY_ID = 1
    OFFICIAL_SYMBOL = 2
    SYNONYM = 3
    CROSS_REFERENCE = 4


class GeneMatch(NamedTuple):
    row: int
    kind: MatchKind


@dataclass(slots=True)
class GeneIndex:
    records: pd.DataFrame
    _taxon_ids: tuple[str, ...]
    _exact: _Tables
    _folded: _Tables | None = None

    def lookup(
        self,
        queries: str | list[str],
        *,
        taxon: str | None = None,
        limit: int | None = 1,
        case_insensitive: bool = False,
    ) -> pd.DataFrame:
        if isinstance(queries, str):
            queries = [queries]
        taxon_id = resolve_taxon(taxon).id if taxon is not None else None

        order: list[int] = []
        query_col: list[str] = []
        kind_col: list[str] = []
        rows: list[int] = []
        miss_order: list[int] = []
        miss_query: list[str] = []

        for i, query in enumerate(queries):
            matches = self._resolve(query, taxon_id, case_insensitive)
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

    def _resolve(self, query: str, taxon_id: str | None, case_insensitive: bool) -> list[GeneMatch]:
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
        if taxon_id is not None:
            matches = [m for m in matches if self._taxon_ids[m.row] == taxon_id]

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
    cross_reference: dict[str, list[int]] = {}

    for i, gene_id in enumerate(records["GeneId"].tolist()):
        primary.setdefault(gene_id, []).append(i)

    for i, symbol in enumerate(records["GeneSymbol"].tolist()):
        if isinstance(symbol, str):
            official.setdefault(symbol, []).append(i)

    for column in ("GeneSynonyms", "GeneSystematicName"):
        for i, value in enumerate(records[column].tolist()):
            if isinstance(value, str):
                for token in value.split("|"):
                    if not token:
                        continue
                    synonym.setdefault(token, []).append(i)

    for i, value in enumerate(records["GeneSecondaryIds"].tolist()):
        if isinstance(value, str):
            for token in value.split("|"):
                if not token:
                    continue
                secondary.setdefault(token, []).append(i)

    for i, value in enumerate(records["GeneCrossReferences"].tolist()):
        if isinstance(value, str):
            for token in value.split("|"):
                if not token or token.split(":", 1)[0] in _XREF_EXCLUDED_PREFIXES:
                    continue
                cross_reference.setdefault(token, []).append(i)

    exact: _Tables = {
        MatchKind.PRIMARY_ID: primary,
        MatchKind.SECONDARY_ID: secondary,
        MatchKind.OFFICIAL_SYMBOL: official,
        MatchKind.SYNONYM: synonym,
        MatchKind.CROSS_REFERENCE: cross_reference,
    }
    return GeneIndex(records, tuple(records["Taxon"].tolist()), exact)


def load_gene_index(path: Path) -> GeneIndex:
    return build_gene_index(load_tsv_gz(path, dtype=str))
