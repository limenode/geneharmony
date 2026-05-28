import gzip
import pickle
from pathlib import Path

import pandas as pd

_DEFAULT_CACHE_DIR = Path(__file__).parent.parent / "cache"

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
        return self.cache_dir.joinpath(*url.strip("/").split("/"))

    def has_dataframes(self, url: str) -> bool:
        return (self._df_dir(url) / "processed.parquet").exists()

    def get_dataframes(self, url: str, load_raw: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
        d = self._df_dir(url)
        processed_df = pd.read_parquet(d / "processed.parquet")
        if load_raw:
            with gzip.open(d / "raw.pkl.gz", "rb") as f:
                raw_df = pickle.load(f)
        else:
            raw_df = pd.DataFrame()
        return processed_df, raw_df

    def set_dataframes(
        self, url: str, processed_df: pd.DataFrame, raw_df: pd.DataFrame
    ) -> None:
        d = self._df_dir(url)
        d.mkdir(parents=True, exist_ok=True)

        # Parquet for processed DataFrames
        processed_df.to_parquet(d / "processed.parquet", compression="zstd", index=False)
        
        # Raw DataFrames contain nested dicts/lists (object dtype) that Parquet
        # cannot represent natively, so pickle + gzip is used instead.
        with gzip.open(d / "raw.pkl.gz", "wb") as f:
            pickle.dump(raw_df, f)