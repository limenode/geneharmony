import asyncio
import httpx

BASE_URL = "https://www.alliancegenome.org/api"

class AsyncAGRClient:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 60.0, max_concurrent: int = 5):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._sem = asyncio.Semaphore(max_concurrent)

    async def get_json(self, path: str, params: dict | None = None):
        async with self._sem:
            r = await self._client.get(path, params=params)
            r.raise_for_status()
            return r.json()
    
    async def get_text(self, path: str, params: dict | None = None):
        async with self._sem:
            r = await self._client.get(path, params=params)
            r.raise_for_status()
            return r.text

    async def close(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

