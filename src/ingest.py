"""Decompress and parse cached AGR bulk download files.

Bulk files are stored gzipped (`.json.gz` / `.tsv.gz`). These helpers read
straight from the compressed file into memory — JSON into the parsed object,
TSV into a DataFrame — so no decompressed copy is written to disk. AGR TSV files
carry a leading block of `#` comment lines before the header, which is skipped.
"""

import gzip
import json
from pathlib import Path
from typing import Any

import pandas as pd


def load_json_gz(path: Path) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def load_tsv_gz(path: Path, dtype: type[str] | None = None) -> pd.DataFrame:
    return pd.read_csv(path, sep="\t", comment="#", compression="gzip", dtype=dtype)
