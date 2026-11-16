"""User-facing entry point: bulk download, per-gene API, and annotate.

`Annotator` ties the lower-level pieces (`AGRClient`, `Downloader`, the gene
index, the dataset registry) into one object over a resolved cache directory. The
intended use is an iterative *filter-then-requery* traversal — one primary AGR
dataset per `annotate` call — so cardinality stays under the caller's control:

    ann = Annotator()
    orth = ann.annotate(["TP53", "BRCA1"], AGRDataset.ORTHOLOGY, taxon="human")
    mouse = orth.loc[orth.Gene2SpeciesTaxonID == "NCBITaxon:10090", "Gene2ID"].unique()
    pheno = ann.annotate(list(mouse), AGRDataset.PHENOTYPES, taxon="mouse")

`annotate` wide-left-joins each source onto the normalized base frame in order:
an AGR dataset contributes its native columns; an ingested external annotation
(referenced by name) contributes columns prefixed with `name.`.
"""

import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final, Self, TypeAlias

import pandas as pd
from platformdirs import user_cache_path

from . import __version__
from .client import AGRClient
from .datasets import DATASETS, AGRDataset, ApiSpec, BulkSpec
from .downloader import Downloader
from .ingest import load_tsv_gz
from .models import DownloadFile
from .normalizer import GeneIndex, build_gene_index, read_gene_index, save_gene_index
from .store import read_metadata, read_parquet, write_parquet
from .taxa import resolve_taxon

Genes: TypeAlias = str | list[str] | pd.DataFrame

_APP_DIR: Final = "geneharmony"
_CACHE_ENV: Final = "GENEHARMONY_CACHE_DIR"
_GENE_ID: Final = "GeneId"
_GENE_INDEX_FILE: Final = "gene_index.parquet"
_PAGE_SIZE: Final = 500
_UNSAFE_FILENAME_CHARS: Final = re.compile(r'[<>:"/\\|?*]')


def default_cache_dir() -> Path:
    """Return `$GENEHARMONY_CACHE_DIR` if set, otherwise the OS's user cache directory."""
    if env := os.environ.get(_CACHE_ENV):
        return Path(env).expanduser()
    return user_cache_path(_APP_DIR, appauthor=False)


def resolve_cache_dir(cache_dir: str | os.PathLike[str] | None) -> Path:
    cache = Path(cache_dir).expanduser() if cache_dir is not None else default_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    return cache

class Annotator:
    def __init__(
        self,
        cache_dir: str | os.PathLike[str] | None = None,
        *,
        client: AGRClient | None = None,
        downloader: Downloader | None = None,
    ) -> None:
        self._cache = resolve_cache_dir(cache_dir)
        self._owns_client = client is None
        self._owns_downloader = downloader is None
        self._client = client if client is not None else AGRClient()
        self._downloader = downloader if downloader is not None else Downloader()
        self._index: GeneIndex | None = None

    def close(self) -> None:
        """Close the HTTP connections this Annotator opened.

        A `client` or `downloader` passed to the constructor is left open.
        """
        if self._owns_client:
            self._client.close()
        if self._owns_downloader:
            self._downloader.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def normalize(
        self,
        genes: str | list[str],
        *,
        taxon: str | None = None,
        limit: int | None = 1,
        case_insensitive: bool = False,
    ) -> pd.DataFrame:
        index = self._gene_index()
        return index.lookup(
            genes,
            taxon=taxon,
            limit=limit,
            case_insensitive=case_insensitive,
        )

    def download(self, dataset: AGRDataset, *, refresh: bool = False) -> Path:
        """Download a dataset's bulk file, convert TSV -> Parquet, drop the .tsv.gz."""
        bulk = DATASETS[dataset].bulk
        if bulk is None:
            raise ValueError(f"{dataset!r} has no bulk file; query it via annotate()")
        dest = self._cache / "bulk" / f"{dataset}.parquet"
        if dest.exists() and not refresh:
            return dest
        file = _select_download(self._client.list_downloads(), bulk)
        tmp = dest.with_name(f"{dest.stem}.tsv.gz")
        self._downloader.download(file.s3Url, tmp)
        write_parquet(
            load_tsv_gz(tmp, dtype=str),
            dest,
            metadata={
                "agr_release": file.releaseVersion,
                "agr_last_modified": file.lastModified.isoformat(),
            },
        )
        tmp.unlink(missing_ok=True)
        return dest

    def ingest_annotation(
        self,
        source: str | Path | pd.DataFrame,
        name: str,
        *,
        gene_id_column: str | list[str],
        normalize: bool = True,
        taxon: str | None = None,
        case_insensitive: bool = False,
        override: bool = False,
    ) -> tuple[dict, pd.DataFrame | None]:
        """Store an external annotation table, keyed by canonical `GeneId`.

        `gene_id_column` may name several columns; they are tried left-to-right
        per row, the first identifier that resolves wins (a fallback for tables
        whose primary ID column has gaps). The id columns are kept as-is and a
        separate `GeneId` column is added for the resolved canonical id, so the
        input must not already contain a `GeneId` column. The returned unmapped
        frame holds the rows (with their original columns) where no candidate
        resolved.
        """
        dest = self._cache / "external" / f"{name}.parquet"
        if dest.exists() and not override:
            return _ingest_summary(name, None, None, None, None), None

        df = source.copy() if isinstance(source, pd.DataFrame) else _read_table(source)
        columns = [gene_id_column] if isinstance(gene_id_column, str) else list(gene_id_column)
        if not columns:
            raise ValueError("gene_id_column must name at least one column")
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise KeyError(
                f"gene_id_column(s) {missing!r} not found; columns are {list(df.columns)}"
            )
        if _GENE_ID in df.columns:
            raise ValueError(
                f"input already has a {_GENE_ID!r} column; rename it — a separate "
                f"{_GENE_ID!r} column is added for the resolved canonical id."
            )

        if normalize:
            str_cols = df[columns].astype(str)
            values = [v for v in pd.unique(str_cols.values.ravel()) if v != "nan"]
            mapping = self._id_map(values, taxon, case_insensitive)
            resolved = str_cols.apply(lambda col: col.map(mapping)).bfill(axis=1).iloc[:, 0]
        else:
            resolved = df[columns].bfill(axis=1).iloc[:, 0]

        df[_GENE_ID] = resolved

        rows_in = len(df)
        rows_dropped = 0
        unmapped_df: pd.DataFrame | None = None
        if normalize:
            unmapped_mask = df[_GENE_ID].isna()
            unmapped_df = df[unmapped_mask].copy()
            rows_dropped = int(unmapped_mask.sum())
            df = df[~unmapped_mask].reset_index(drop=True)

        write_parquet(df, dest)
        return _ingest_summary(name, rows_in, len(df), rows_dropped, normalize), unmapped_df

    def annotate(
        self,
        genes: Genes,
        *sources: AGRDataset | str,
        taxon: str | None = None,
        limit: int | None = 1,
        case_insensitive: bool = False,
    ) -> pd.DataFrame:
        if isinstance(genes, pd.DataFrame):
            base = genes.copy()
        else:
            base = self.normalize(
                genes, taxon=taxon, limit=limit, case_insensitive=case_insensitive
            )
        if _GENE_ID not in base.columns:
            raise KeyError(f"base frame has no {_GENE_ID!r} column to join on")

        gene_ids = base[_GENE_ID].dropna().unique().tolist()
        out = base
        for source in sources:
            if isinstance(source, AGRDataset):
                frame, key = self._load_agr_source(source, gene_ids)
            else:
                frame, key = self._load_external(source)
            frame[f'has.{source}'] = True
            out = out.merge(frame, how="left", left_on=_GENE_ID, right_on=key)
            out[f'has.{source}'] = out[f'has.{source}'].fillna(False)
        return out.reset_index(drop=True)

    def get_orthologs(
        self,
        genes: Genes,
        taxon: str | None = None,
        target_taxon: str | None = None,
        limit: int | None = 1,
        case_insensitive: bool = False
    ) -> pd.DataFrame:
        """Convenience method to get orthologs for a set of genes."""
        df = self.annotate(
            genes,
            AGRDataset.ORTHOLOGY,
            taxon=taxon,
            limit=limit,
            case_insensitive=case_insensitive,
        )
        if target_taxon:
            df = df[df["Gene2SpeciesTaxonID"] == resolve_taxon(target_taxon).id]

        return df[["query", "match_kind", "Gene2ID", "Gene2Symbol", "Gene2SpeciesTaxonID"]]


    def _gene_index(self) -> GeneIndex:
        """Load the gene index, caching it for the Annotator's lifetime.

        The index is built from `bulk/gene.parquet` and saved to
        `bulk/gene_index.parquet`, stamped with the AGR release of the gene file
        and the geneharmony version. A saved index is reused only when its stamp
        matches; otherwise it is rebuilt and saved again.
        """
        if self._index is None:
            gene_path = self.download(AGRDataset.GENE)
            records = read_parquet(gene_path)
            gene_meta = read_metadata(gene_path)
            stamp = {
                "agr_release": gene_meta.get("agr_release", "unknown"),
                "agr_last_modified": gene_meta.get("agr_last_modified", "unknown"),
                "geneharmony_version": __version__,
                "gene_rows": str(len(records)),
            }
            index_path = self._cache / "bulk" / _GENE_INDEX_FILE
            self._index = _read_saved_index(records, index_path, stamp)
            if self._index is None:
                self._index = build_gene_index(records)
                save_gene_index(self._index, index_path, stamp)
        return self._index

    def _id_map(
        self, queries: list[str], taxon: str | None, case_insensitive: bool = False
    ) -> dict[str, str]:
        index = self._gene_index()
        unique = list(dict.fromkeys(queries))
        df = index.lookup(unique, taxon=taxon, limit=1, case_insensitive=case_insensitive)
        df = df[df["match_kind"].notna()]
        return dict(zip(df["query"], df[_GENE_ID]))

    def _load_agr_source(
        self, dataset: AGRDataset, gene_ids: list[str]
    ) -> tuple[pd.DataFrame, str]:
        spec = DATASETS[dataset]
        if spec.bulk is not None:
            path = self._cache / "bulk" / f"{dataset}.parquet"
            if not path.exists():
                self.download(dataset)
            frame = read_parquet(path)
            key = spec.bulk.join_key
            return frame[frame[key].isin(set(gene_ids))].reset_index(drop=True), key
        assert spec.api is not None
        return self._fetch_api(dataset, spec.api, gene_ids), spec.api.join_key

    def _fetch_api(
        self, dataset: AGRDataset, api: ApiSpec, gene_ids: list[str]
    ) -> pd.DataFrame:
        api_dir = self._cache / "api" / dataset
        paths = {g: api_dir / f"{_safe(g)}.parquet" for g in gene_ids}
        missing = [g for g, path in paths.items() if not path.exists()]

        frames = [read_parquet(path) for g, path in paths.items() if g not in set(missing)]
        endpoints = [api.endpoint.format(gene_id=g) for g in missing]
        for gene_id, results in zip(missing, _fetch_all_pages(self._client, endpoints)):
            rows = [api.project(gene_id, r) for r in results]
            frame = pd.DataFrame(rows) if rows else pd.DataFrame(columns=[api.join_key])
            write_parquet(frame, paths[gene_id])
            frames.append(frame)
        if not frames:
            return pd.DataFrame(columns=[api.join_key])
        return pd.concat(frames, ignore_index=True)

    def _load_external(self, name: str) -> tuple[pd.DataFrame, str]:
        path = self._cache / "external" / f"{name}.parquet"
        if not path.exists():
            raise KeyError(
                f"unknown source {name!r}: not an AGRDataset or an ingested annotation"
            )
        frame = read_parquet(path)
        frame = frame.rename(
            columns={c: f"{name}.{c}" for c in frame.columns if c != _GENE_ID}
        )
        return frame, _GENE_ID


def _read_saved_index(
    records: pd.DataFrame, path: Path, stamp: dict[str, str]
) -> GeneIndex | None:
    try:
        if read_metadata(path) != stamp:
            return None
        return read_gene_index(records, path)
    except (OSError, ValueError):
        return None


def _select_download(files: list[DownloadFile], spec: BulkSpec) -> DownloadFile:
    match = next(
        (
            f
            for f in files
            if f.dataType == spec.data_type
            and f.fileType == spec.file_type
            and f.dataSubType == spec.data_sub_type
        ),
        None,
    )
    if match is None:
        raise LookupError(
            f"no download matching {spec.data_type}/{spec.file_type}/{spec.data_sub_type}"
        )
    return match


def _fetch_all_pages(
    client: AGRClient, endpoints: list[str], page_size: int = _PAGE_SIZE
) -> list[list[dict]]:
    """Fetch every page of each endpoint, returning one result list per endpoint.

    Runs in two rounds on one thread pool: the first page of every endpoint
    (which gives each total), then all remaining pages.
    """
    if not endpoints:
        return []

    def get(endpoint: str, page: int) -> Any:
        return client.get_json(endpoint, params={"limit": page_size, "page": page})

    pool = ThreadPoolExecutor(max_workers=client.max_concurrent)
    try:
        firsts = list(pool.map(get, endpoints, [1] * len(endpoints)))
        results = [list(first.get("results", [])) for first in firsts]
        rest = [
            (i, page)
            for i, first in enumerate(firsts)
            for page in range(2, math.ceil(first.get("total", len(results[i])) / page_size) + 1)
        ]
        pages = pool.map(get, [endpoints[i] for i, _ in rest], [page for _, page in rest])
        for (i, _), page in zip(rest, pages):
            results[i].extend(page.get("results", []))
    finally:
        pool.shutdown(cancel_futures=True)
    return results


def _safe(gene_id: str) -> str:
    return _UNSAFE_FILENAME_CHARS.sub("_", gene_id)


def _read_table(path: str | Path) -> pd.DataFrame:
    suffix = Path(path).suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(path)
    if suffix in (".tsv", ".tab", ".txt"):
        return pd.read_csv(path, sep="\t")
    if suffix == ".csv":
        return pd.read_csv(path)
    raise ValueError(
        f"unsupported annotation file type {suffix!r} for {path!r} "
        "(expected .csv, .tsv/.tab/.txt, or .parquet)"
    )


def _ingest_summary(
    name: str,
    rows_in: int | None,
    rows_stored: int | None,
    rows_dropped: int | None,
    normalized: bool | None,
) -> dict:
    return {
        "annotation_name": name,
        "rows_in": rows_in,
        "rows_stored": rows_stored,
        "rows_dropped_unmapped": rows_dropped,
        "normalized": normalized,
    }
