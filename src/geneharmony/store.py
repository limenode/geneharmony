"""Atomic Parquet persistence for cached frames.

Backs the bulk, per-gene API, and external-annotation caches. Writes go through a
same-directory temp file then `os.replace`, so a reader never sees a half-written
file. Object columns holding dicts/lists are JSON-encoded to strings before
writing (Parquet can't represent them natively); `read_parquet(decode_json=...)`
reverses that for the columns a caller knows were nested.
"""

import json
import os
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pandas as pd


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


def write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _encode_nested(df)
    _atomic_write(path, lambda p: encoded.to_parquet(p, compression="zstd", index=False))


def read_parquet(path: Path, *, decode_json: Iterable[str] = ()) -> pd.DataFrame:
    df = pd.read_parquet(path)
    for col in decode_json:
        if col in df.columns:
            df[col] = [None if _is_missing(v) else json.loads(v) for v in df[col]]
    return df
