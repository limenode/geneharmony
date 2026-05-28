import asyncio
import httpx

from cache import CacheManager

BASE_URL = "https://www.alliancegenome.org/api"

class AsyncAGRClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 60.0,
        max_concurrent: int = 5,
        cache: CacheManager | None = None,
    ):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._sem = asyncio.Semaphore(max_concurrent)
        self._cache = cache

    async def get_json(self, path: str, params: dict | None = None) -> dict:
        if self._cache:
            cached = self._cache.get_json(path, params)
            if cached is not None:
                return cached

        async with self._sem:
            r = await self._client.get(path, params=params)
            r.raise_for_status()
            data = r.json()

        if self._cache:
            self._cache.set_json(path, params, data)

        return data

    async def get_text(self, path: str, params: dict | None = None) -> str:
        if self._cache:
            cached = self._cache.get_text(path, params)
            if cached is not None:
                return cached

        async with self._sem:
            r = await self._client.get(path, params=params)
            r.raise_for_status()
            data = r.text

        if self._cache:
            self._cache.set_text(path, params, data)

        return data

    async def close(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

