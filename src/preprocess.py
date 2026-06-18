"""Resolve a cache directory and prepare a ready-to-use `GeneIndex`.

`prepare_gene_index` is the user-facing preprocessing step. It hides the bulk
download and index build behind a layered cache rooted at `cache_dir` (the
user's home cache by default), trying the cheapest source first:

    gene.parquet     records DataFrame      — skips re-parsing the gzipped TSV
    gene.tsv.gz      raw AGR bulk download   — converted to parquet on first read

The index itself is rebuilt from the records each call: the build is only a
couple of seconds, and the dict tables don't serialize compactly (see below).
On a cold cache it downloads `GENE-TSV-COMBINED`, writes the parquet for next
time, and returns the index. `refresh=True` bypasses every layer and
re-downloads. An `AGRClient` / `Downloader` are created only when a download is
actually needed; passing your own leaves their lifecycle to you.
"""

import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Final

import pandas as pd

from client import AGRClient
from downloader import Downloader
from ingest import load_tsv_gz
from models import DownloadFile
from normalizer import GeneIndex, build_gene_index

_APP_DIR: Final = "alliance_wrapper"
_GENE_TSV: Final = "gene.tsv.gz"
_GENE_PARQUET: Final = "gene.parquet"


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / _APP_DIR


def resolve_cache_dir(cache_dir: Path | None) -> Path:
    cache = cache_dir or default_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    return cache


async def prepare_gene_index(
    cache_dir: Path | None = None,
    *,
    refresh: bool = False,
    client: AGRClient | None = None,
    downloader: Downloader | None = None,
) -> GeneIndex:
    """Return a `GeneIndex`, building and caching it under `cache_dir` as needed."""
    cache = resolve_cache_dir(cache_dir)

    records = await _load_gene_records(
        cache, refresh=refresh, client=client, downloader=downloader
    )
    return build_gene_index(records)


async def _load_gene_records(
    cache: Path,
    *,
    refresh: bool,
    client: AGRClient | None,
    downloader: Downloader | None,
) -> pd.DataFrame:
    parquet_path = cache / _GENE_PARQUET
    if not refresh and parquet_path.exists():
        return pd.read_parquet(parquet_path)

    tsv_path = cache / _GENE_TSV
    if refresh or not tsv_path.exists():
        await _download_gene_tsv(tsv_path, client=client, downloader=downloader)

    records = load_tsv_gz(tsv_path, dtype=str)
    records.to_parquet(parquet_path)
    return records


async def _download_gene_tsv(
    dest: Path, *, client: AGRClient | None, downloader: Downloader | None
) -> None:
    async with AsyncExitStack() as stack:
        if client is None:
            client = await stack.enter_async_context(AGRClient())
        if downloader is None:
            downloader = await stack.enter_async_context(Downloader())
        gene_file = _find_gene_file(await client.list_downloads())
        await downloader.download(gene_file.s3Url, dest)


def _find_gene_file(files: list[DownloadFile]) -> DownloadFile:
    gene_file = next(
        (
            f
            for f in files
            if f.dataType == "GENE" and f.dataSubType == "COMBINED" and f.fileType == "TSV"
        ),
        None,
    )
    if gene_file is None:
        raise LookupError("GENE-TSV-COMBINED not found in the /downloads listing")
    return gene_file
