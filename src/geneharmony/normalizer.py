"""In-memory gene normalizer built from the GENE-TSV-COMBINED bulk file.

`build_gene_index` flattens every identifier form — primary ID, deprecated
(secondary) ID, official symbol, synonym, systematic name and external
cross-reference (e.g. `NCBI_Gene:`, `ENSEMBL:`, `UniProtKB:`) — into one long
Arrow table of `(key, row, kind)`, using pyarrow compute kernels. A lookup is a
vectorized `is_in` scan of that table, so a batch of queries costs one pass.

`GeneIndex.lookup` takes one query or a list and returns a DataFrame with one
row per match: the original `query`, the `match_kind`, and every column of the
matched gene record. Matches are ranked by precedence (primary ID > secondary ID
> official symbol > synonym > cross-reference); `limit` caps matches per query and `taxon` narrows
symbols that recur across species. Unmatched queries are still returned, with a
null `match_kind`. Matching is case-sensitive unless `case_insensitive=True`,
since case can be meaningful across species (human TP53 vs mouse Trp53).
"""

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .ingest import load_tsv_gz
from .store import write_table
from .taxa import resolve_taxon

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


_KIND_NAMES: Final = {kind.value: kind.name for kind in MatchKind}

# (column, kind, pipe-separated?) — the identifier columns of GENE-TSV-COMBINED.
_KEY_COLUMNS: Final = (
    ("GeneId", MatchKind.PRIMARY_ID, False),
    ("GeneSecondaryIds", MatchKind.SECONDARY_ID, True),
    ("GeneSymbol", MatchKind.OFFICIAL_SYMBOL, False),
    ("GeneSynonyms", MatchKind.SYNONYM, True),
    ("GeneSystematicName", MatchKind.SYNONYM, True),
    ("GeneCrossReferences", MatchKind.CROSS_REFERENCE, True),
)


@dataclass(slots=True)
class GeneIndex:
    records: pd.DataFrame
    _keys: pa.Table
    _folded: pa.ChunkedArray | None = None

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
        query_keys = pa.array(queries, pa.string())
        keys = self._keys["key"]
        if case_insensitive:
            query_keys = pc.utf8_lower(query_keys)
            keys = self._folded_keys()

        mask = pc.is_in(keys, value_set=pc.unique(query_keys))
        hits = pd.DataFrame(
            {
                "key": keys.filter(mask).to_pandas(),
                "row": self._keys["row"].filter(mask).to_pandas(),
                "kind": self._keys["kind"].filter(mask).to_pandas(),
            }
        )
        if taxon is not None:
            taxon_of_row = self.records["Taxon"].iloc[hits["row"]].to_numpy()
            hits = hits[taxon_of_row == resolve_taxon(taxon).id]

        wanted = pd.DataFrame(
            {"_order": range(len(queries)), "query": queries, "key": query_keys.to_pandas()}
        )
        matches = (
            wanted.merge(hits, on="key")
            .sort_values(["_order", "kind", "row"])
            .drop_duplicates(["_order", "row"])
        )
        if limit is not None:
            matches = matches[matches.groupby("_order").cumcount() < limit]

        matched = self.records.iloc[matches["row"]].reset_index(drop=True)
        matched.insert(0, "match_kind", matches["kind"].map(_KIND_NAMES).to_numpy())
        matched.insert(0, "query", matches["query"].to_numpy())
        matched.insert(0, "_order", matches["_order"].to_numpy())

        missed = wanted.loc[~wanted["_order"].isin(matches["_order"]), ["_order", "query"]]
        if not missed.empty:
            matched = pd.concat([matched, missed.assign(match_kind=None)], ignore_index=True)

        return (
            matched.sort_values("_order", kind="stable")
            .drop(columns="_order")
            .reset_index(drop=True)
        )

    def _folded_keys(self) -> pa.ChunkedArray:
        if self._folded is None:
            self._folded = pc.utf8_lower(self._keys["key"])
        return self._folded


def _key_part(values: pa.Array, kind: MatchKind, pipe_separated: bool) -> pa.Table:
    if pipe_separated:
        lists = pc.split_pattern(values, "|")
        keys = pc.list_flatten(lists)
        rows = pc.list_parent_indices(lists)
        keep = pc.not_equal(keys, "")
        if kind is MatchKind.CROSS_REFERENCE:
            prefixes = pc.list_element(pc.split_pattern(keys, ":", max_splits=1), 0)
            excluded = pc.is_in(prefixes, value_set=pa.array(sorted(_XREF_EXCLUDED_PREFIXES)))
            keep = pc.and_(keep, pc.invert(excluded))
        keys, rows = keys.filter(keep), rows.filter(keep)
    else:
        rows = pc.indices_nonzero(pc.is_valid(values))
        keys = values.take(rows)
    return pa.table(
        {
            "key": keys.cast(pa.string()),
            "row": rows.cast(pa.int32()),
            "kind": pa.repeat(pa.scalar(kind.value, pa.int8()), len(keys)),
        }
    )


def build_gene_index(records: pd.DataFrame) -> GeneIndex:
    table = pa.Table.from_pandas(
        records[[column for column, _, _ in _KEY_COLUMNS]], preserve_index=False
    )
    parts = [
        _key_part(table[column].combine_chunks(), kind, pipe_separated)
        for column, kind, pipe_separated in _KEY_COLUMNS
    ]
    return GeneIndex(records, pa.concat_tables(parts).combine_chunks())


def load_gene_index(path: Path) -> GeneIndex:
    return build_gene_index(load_tsv_gz(path, dtype=str))


def save_gene_index(index: GeneIndex, path: Path, metadata: Mapping[str, str]) -> None:
    """Write the index's key table to Parquet; the records are not included."""
    write_table(index._keys, path, metadata=metadata)


def read_gene_index(records: pd.DataFrame, path: Path) -> GeneIndex:
    """Rebuild a `GeneIndex` from a key table written by `save_gene_index`.

    `records` must be the same table the index was built from.
    """
    return GeneIndex(records, pq.read_table(path))
