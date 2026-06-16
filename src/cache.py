import gzip
import json
import os
import pickle
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

_DEFAULT_CACHE_DIR = Path(__file__).parent.parent / "cache"

# Bump when the on-disk shape of a cache entry changes
_CACHE_VERSION = 2

def _atomic_write(path: Path, write_fn: Callable[[Path], None]) -> None:
    """Write ``path`` via a temp file in the same dir, then rename."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        write_fn(tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

class CacheManager:
    def __init__(self, cache_dir: str | Path = _DEFAULT_CACHE_DIR):
        self.cache_dir = Path(cache_dir)

    def _path(self, api_path: str, params: dict | None, suffix: str) -> Path:
        segments = api_path.strip("/").split("/")

        # Sorted params become the filename so that {page: 1, limit: 500} and
        # {limit: 500, page: 1} always resolve to the same file.
        if params:
            filename = "&".join(f"{k}={v}" for k, v in sorted(params.items())) + suffix
        else:
            filename = "response" + suffix

        return self.cache_dir.joinpath(*segments) / filename
    
    def exists(self, api_path: str, params: dict | None) -> bool:
        return self._path(api_path, params, ".cache").exists()

    def _df_dir(self, url: str) -> Path:
        """Convert a URL path into the directory where its cache files are stored."""
        return self.cache_dir.joinpath(*url.strip("/").split("/"))

    def has_dataframes(self, url: str) -> bool:
        # An entry only counts as a hit if its completed parquet exists *and* it
        # was written by the current cache version. A version mismatch (or a
        # missing meta.json) is treated as a miss so callers re-fetch and
        # overwrite the stale entry, rather than concatenating incompatible schemas.
        if not (self._df_dir(url) / "processed.parquet").exists():
            return False
        meta = self.get_meta(url)
        return bool(meta) and meta.get("cache_version") == _CACHE_VERSION

    def get_dataframes(self, url: str, load_raw: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Load the processed and raw DataFrames for a URL from cache. Raises if not present."""
        d = self._df_dir(url)
        processed_df = pd.read_parquet(d / "processed.parquet")
        if load_raw:
            with gzip.open(d / "raw.pkl.gz", "rb") as f:
                raw_df = pickle.load(f)
        else:
            raw_df = pd.DataFrame()
        return processed_df, raw_df

    def get_meta(self, url: str) -> dict | None:
        """Return the sidecar metadata for an entry, or None if absent."""
        meta_path = self._df_dir(url) / "meta.json"
        if not meta_path.exists():
            return None
        return json.loads(meta_path.read_text())

    def set_dataframes(
        self, url: str, processed_df: pd.DataFrame, raw_df: pd.DataFrame
    ) -> None:
        """Persist the processed and raw DataFrames for a URL to cache, along with sidecar metadata."""
        d = self._df_dir(url)
        d.mkdir(parents=True, exist_ok=True)

        # Raw DataFrames contain nested dicts/lists (object dtype) that Parquet
        # cannot represent natively, so pickle + gzip is used instead.
        def _write_raw(p: Path) -> None:
            with gzip.open(p, "wb") as f:
                pickle.dump(raw_df, f)

        def _write_meta(p: Path) -> None:
            p.write_text(json.dumps(meta, indent=2))

        def _write_processed(p: Path) -> None:
            processed_df.to_parquet(p, compression="zstd", index=False)

        meta = {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "cache_version": _CACHE_VERSION,
            "processed_rows": int(len(processed_df)),
            "raw_rows": int(len(raw_df)),
        }

        _atomic_write(d / "raw.pkl.gz", _write_raw)
        _atomic_write(d / "meta.json", _write_meta)
        _atomic_write(d / "processed.parquet", _write_processed)
    
    def set_annotation(self, name: str, df: pd.DataFrame) -> None:
        """Persist an annotation DataFrame to cache."""
        d = self.cache_dir / "external"
        d.mkdir(parents=True, exist_ok=True)

        def _write(p: Path) -> None:
            df.to_parquet(p, compression="zstd", index=False)

        _atomic_write(d / f"{name}.parquet", _write)
    
    def get_annotation(self, name: str) -> pd.DataFrame:
        """Load an annotation DataFrame from cache. Raises if not present."""
        return pd.read_parquet(self.cache_dir / "external" / f"{name}.parquet")

    def list_annotations(self) -> list[str]:
        """List the names of all cached annotations."""
        d = self.cache_dir / "external"
        if not d.exists():
            return []
        return [p.stem for p in d.glob("*.parquet")]

    