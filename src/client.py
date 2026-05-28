import asyncio
import json
import httpx

BASE_URL = "https://www.alliancegenome.org/api"

class AsyncAGRClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 60.0,
        max_concurrent: int = 5
    ):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._sem = asyncio.Semaphore(max_concurrent)

    async def get_data(self, path: str, params: dict | None = None) -> str:
        async with self._sem:
            result = await self._client.get(path, params=params)
            result.raise_for_status()

        return result.text

    async def get_json(self, path: str, params: dict | None = None) -> dict:
        text = await self.get_data(path, params)
        return json.loads(text)

    async def close(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

