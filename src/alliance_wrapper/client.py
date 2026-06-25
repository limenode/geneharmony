"""Async HTTP client for the Alliance of Genome Resources REST API.

A single `AGRClient` owns one pooled `httpx.AsyncClient` and bounds in-flight
requests with a semaphore. GETs retry transient failures (429/5xx, timeouts,
transport errors) with exponential backoff and jitter, honoring `Retry-After`.
"""

import asyncio
import random
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Final, Self

import httpx
from pydantic import TypeAdapter

from .models import DownloadFile

AGR_BASE_URL: Final = "https://www.alliancegenome.org/api"

type Params = Mapping[str, str | int | bool]

_RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({429, 502, 503, 504})
_DOWNLOADS_ADAPTER: Final = TypeAdapter(list[DownloadFile])


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    if value.isdigit():
        return float(value)
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


class AGRClient:
    def __init__(
        self,
        base_url: str = AGR_BASE_URL,
        *,
        max_concurrent: int = 5,
        timeout: httpx.Timeout = httpx.Timeout(10.0, read=120.0),
        max_retries: int = 4,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            limits=httpx.Limits(max_connections=max_concurrent),
        )
        self._sem = asyncio.Semaphore(max_concurrent)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap

    async def get_json(self, path: str, params: Params | None = None) -> Any:
        return (await self._get(path, params)).json()

    async def get_text(self, path: str, params: Params | None = None) -> str:
        return (await self._get(path, params)).text

    async def list_downloads(self) -> list[DownloadFile]:
        return _DOWNLOADS_ADAPTER.validate_python(await self.get_json("/downloads"))

    async def _get(self, path: str, params: Params | None) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            last = attempt == self._max_retries
            try:
                async with self._sem:
                    response = await self._client.get(path, params=params)
            except (httpx.TransportError, httpx.TimeoutException):
                if last:
                    raise
                delay = self._backoff(attempt)
            else:
                if response.status_code not in _RETRYABLE_STATUS or last:
                    response.raise_for_status()
                    return response
                delay = _parse_retry_after(response.headers.get("Retry-After"))
                if delay is None:
                    delay = self._backoff(attempt)
            await asyncio.sleep(delay)
        raise AssertionError("retry loop exited without returning")

    def _backoff(self, attempt: int) -> float:
        return random.uniform(0.0, min(self._backoff_cap, self._backoff_base * 2**attempt))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()
