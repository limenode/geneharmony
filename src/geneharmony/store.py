"""Atomic Parquet persistence for cached frames.

Backs the bulk, per-gene API, and external-annotation caches. Writes go through a
same-directory temp file then `os.replace`, so a reader never sees a half-written
file. Object columns holding dicts/lists are JSON-encoded to strings before
writing (Parquet can't represent them natively); `read_parquet(decode_json=...)`
reverses that for the columns a caller knows were nested.

Files can carry string key/value metadata in the Parquet footer (e.g. the AGR
release a bulk file came from), readable without loading the data.
"""

import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Final

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

_METADATA_PREFIX: Final = "geneharmony."


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value))


def _encode_nested(df: pd.DataFrame) -> pd.DataFrame:
    nested = [
        col
        for col in df.columns
        if isinstance(next((v for v in df[col] if not _is_missing(v)), None), (dict, list))
    ]
    if not nested:
        return df
    out = df.copy()
    for col in nested:
        out[col] = [None if _is_missing(v) else json.dumps(v) for v in out[col]]
    return out


def _atomic_write(path: Path, write_fn: Callable[[Path], None]) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        write_fn(tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_parquet(
    df: pd.DataFrame, path: Path, *, metadata: Mapping[str, str] | None = None
) -> None:
    table = pa.Table.from_pandas(_encode_nested(df), preserve_index=False)
    write_table(table, path, metadata=metadata)


def write_table(
    table: pa.Table, path: Path, *, metadata: Mapping[str, str] | None = None
) -> None:
    if metadata:
        tagged = {f"{_METADATA_PREFIX}{k}".encode(): v.encode() for k, v in metadata.items()}
        table = table.replace_schema_metadata({**(table.schema.metadata or {}), **tagged})
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, lambda p: pq.write_table(table, p, compression="zstd"))


def read_metadata(path: Path) -> dict[str, str]:
    """Return the geneharmony metadata stored in a Parquet file's footer."""
    raw = pq.read_schema(path).metadata or {}
    return {
        k.decode().removeprefix(_METADATA_PREFIX): v.decode()
        for k, v in raw.items()
        if k.decode().startswith(_METADATA_PREFIX)
    }


def read_parquet(path: Path, *, decode_json: Iterable[str] = ()) -> pd.DataFrame:
    df = pd.read_parquet(path)
    for col in decode_json:
        if col in df.columns:
            df[col] = [None if _is_missing(v) else json.loads(v) for v in df[col]]
    return df
