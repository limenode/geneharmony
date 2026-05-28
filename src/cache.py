import json
from pathlib import Path

# Default cache root sits at the project root (one level above src/), keeping
# cached data out of the source tree.
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

    def get_json(self, api_path: str, params: dict | None) -> dict | None:
        p = self._path(api_path, params, ".json")
        if p.exists():
            return json.loads(p.read_text())
        return None

    def set_json(self, api_path: str, params: dict | None, data: dict) -> None:
        p = self._path(api_path, params, ".json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))

    def get_text(self, api_path: str, params: dict | None) -> str | None:
        p = self._path(api_path, params, ".tsv")
        if p.exists():
            return p.read_text()
        return None

    def set_text(self, api_path: str, params: dict | None, data: str) -> None:
        p = self._path(api_path, params, ".tsv")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)
