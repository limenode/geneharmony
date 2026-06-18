"""User-facing entry point: bulk download, per-gene API, and annotate.

`Annotator` ties the lower-level pieces (`AGRClient`, `Downloader`, the gene
index, the dataset registry) into one object over a resolved cache directory. The
intended use is an iterative *filter-then-requery* traversal — one primary AGR
dataset per `annotate` call — so cardinality stays under the caller's control:

    ann = Annotator()
    orth = await ann.annotate(["TP53", "BRCA1"], AGRDataset.ORTHOLOGY, taxon="human")
    mouse = orth.loc[orth.Gene2SpeciesTaxonID == "NCBITaxon:10090", "Gene2ID"].unique()
    pheno = await ann.annotate(list(mouse), AGRDataset.PHENOTYPES, taxon="mouse")

`annotate` wide-left-joins each source onto the normalized base frame in order:
an AGR dataset contributes its native columns; an ingested external annotation
(referenced by name) contributes columns prefixed with `name.`.
"""

import asyncio
import math
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Final

import pandas as pd

from client import AGRClient
from datasets import DATASETS, AGRDataset, ApiSpec, BulkSpec
from downloader import Downloader
from ingest import load_tsv_gz
from models import DownloadFile
from normalizer import GeneIndex
from preprocess import prepare_gene_index, resolve_cache_dir
from store import read_parquet, write_parquet

type Genes = str | list[str] | pd.DataFrame

_GENE_ID: Final = "GeneId"
_PAGE_SIZE: Final = 500


class Annotator:
    def __init__(
        self,
        cache_dir: Path | None = None,
        *,
        client: AGRClient | None = None,
        downloader: Downloader | None = None,
    ) -> None:
        self._cache = resolve_cache_dir(cache_dir)
        self._client = client
        self._downloader = downloader
        self._index: GeneIndex | None = None

    async def normalize(
        self,
        genes: str | list[str],
        *,
        taxon: str | None = None,
        limit: int | None = 1,
        case_insensitive: bool = False,
    ) -> pd.DataFrame:
        index = await self._gene_index()
        return index.normalize(
            genes,
            taxon=taxon,
            limit=limit,
            case_insensitive=case_insensitive,
        )

    async def download(self, dataset: AGRDataset, *, refresh: bool = False) -> Path:
        """Download a dataset's bulk file, convert TSV -> Parquet, drop the .tsv.gz."""
        bulk = DATASETS[dataset].bulk
        if bulk is None:
            raise ValueError(f"{dataset!r} has no bulk file; query it via annotate()")
        dest = self._cache / "bulk" / f"{dataset}.parquet"
        if dest.exists() and not refresh:
            return dest
        async with AsyncExitStack() as stack:
            client = self._client or await stack.enter_async_context(AGRClient())
            downloader = self._downloader or await stack.enter_async_context(Downloader())
            file = _select_download(await client.list_downloads(), bulk)
            tmp = self._cache / "bulk" / f"{dataset}.tsv.gz"
            await downloader.download(file.s3Url, tmp)
            write_parquet(load_tsv_gz(tmp, dtype=str), dest)
            tmp.unlink(missing_ok=True)
        return dest

    async def ingest_annotation(
        self,
        source: str | Path | pd.DataFrame,
        name: str,
        *,
        gene_id_column: str,
        normalize: bool = True,
        taxon: str | None = None,
        case_insensitive: bool = False,
        override: bool = False,
    ) -> dict:
        """Store an external annotation table, keyed by canonical `GeneId`."""
        dest = self._cache / "external" / f"{name}.parquet"
        if dest.exists() and not override:
            return _ingest_summary(name, None, None, None, None)

        df = source.copy() if isinstance(source, pd.DataFrame) else _read_table(source)
        if gene_id_column not in df.columns:
            raise KeyError(
                f"gene_id_column {gene_id_column!r} not found; columns are {list(df.columns)}"
            )
        if gene_id_column != _GENE_ID and _GENE_ID in df.columns:
            raise ValueError(
                f"input already has a {_GENE_ID!r} column distinct from {gene_id_column!r}; "
                "rename it to avoid a collision."
            )
        df = df.rename(columns={gene_id_column: _GENE_ID})

        rows_in = len(df)
        rows_dropped = 0
        if normalize:
            mapping = await self._id_map(
                df[_GENE_ID].astype(str).tolist(), taxon, case_insensitive
            )
            df[_GENE_ID] = df[_GENE_ID].astype(str).map(mapping)
            unmapped = df[_GENE_ID].isna()
            rows_dropped = int(unmapped.sum())
            df = df[~unmapped].reset_index(drop=True)

        write_parquet(df, dest)
        return _ingest_summary(name, rows_in, len(df), rows_dropped, normalize)

    async def annotate(
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
            base = await self.normalize(
                genes, taxon=taxon, limit=limit, case_insensitive=case_insensitive
            )
        if _GENE_ID not in base.columns:
            raise KeyError(f"base frame has no {_GENE_ID!r} column to join on")

        gene_ids = base[_GENE_ID].dropna().unique().tolist()
        out = base
        for source in sources:
            if isinstance(source, AGRDataset):
                frame, key = await self._load_agr_source(source, gene_ids)
            else:
                frame, key = self._load_external(source)
            out = out.merge(frame, how="left", left_on=_GENE_ID, right_on=key)
            if key != _GENE_ID:
                out = out.drop(columns=key)
        return out.reset_index(drop=True)

    async def _gene_index(self) -> GeneIndex:
        if self._index is None:
            self._index = await prepare_gene_index(
                self._cache, client=self._client, downloader=self._downloader
            )
        return self._index

    async def _id_map(
        self, queries: list[str], taxon: str | None, case_insensitive: bool = False
    ) -> dict[str, str]:
        index = await self._gene_index()
        unique = list(dict.fromkeys(queries))
        df = index.normalize(unique, taxon=taxon, limit=1, case_insensitive=case_insensitive)
        df = df[df["match_kind"].notna()]
        return dict(zip(df["query"], df[_GENE_ID]))

    async def _load_agr_source(
        self, dataset: AGRDataset, gene_ids: list[str]
    ) -> tuple[pd.DataFrame, str]:
        spec = DATASETS[dataset]
        if spec.bulk is not None:
            path = self._cache / "bulk" / f"{dataset}.parquet"
            if not path.exists():
                await self.download(dataset)
            frame = read_parquet(path)
            key = spec.bulk.join_key
            return frame[frame[key].isin(set(gene_ids))].reset_index(drop=True), key
        assert spec.api is not None
        return await self._fetch_api(dataset, spec.api, gene_ids), spec.api.join_key

    async def _fetch_api(
        self, dataset: AGRDataset, api: ApiSpec, gene_ids: list[str]
    ) -> pd.DataFrame:
        api_dir = self._cache / "api" / dataset
        cached = [g for g in gene_ids if (api_dir / f"{_safe(g)}.parquet").exists()]
        missing = [g for g in gene_ids if g not in set(cached)]

        frames = [read_parquet(api_dir / f"{_safe(g)}.parquet") for g in cached]
        if missing:
            async with AsyncExitStack() as stack:
                client = self._client or await stack.enter_async_context(AGRClient())
                frames.extend(
                    await asyncio.gather(
                        *[self._fetch_one(client, api, g, api_dir) for g in missing]
                    )
                )
        if not frames:
            return pd.DataFrame(columns=[api.join_key])
        return pd.concat(frames, ignore_index=True)

    async def _fetch_one(
        self, client: AGRClient, api: ApiSpec, gene_id: str, api_dir: Path
    ) -> pd.DataFrame:
        results = await _fetch_all_pages(client, api.endpoint.format(gene_id=gene_id))
        rows = [api.project(gene_id, r) for r in results]
        frame = pd.DataFrame(rows) if rows else pd.DataFrame(columns=[api.join_key])
        write_parquet(frame, api_dir / f"{_safe(gene_id)}.parquet")
        return frame

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


async def _fetch_all_pages(
    client: AGRClient, endpoint: str, page_size: int = _PAGE_SIZE
) -> list[dict]:
    first = await client.get_json(endpoint, params={"limit": page_size, "page": 1})
    results = list(first.get("results", []))
    pages = math.ceil(first.get("total", len(results)) / page_size)
    if pages > 1:
        rest = await asyncio.gather(
            *[
                client.get_json(endpoint, params={"limit": page_size, "page": p})
                for p in range(2, pages + 1)
            ]
        )
        for page in rest:
            results.extend(page.get("results", []))
    return results


def _safe(gene_id: str) -> str:
    return gene_id.replace(":", "_").replace("/", "_")


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
