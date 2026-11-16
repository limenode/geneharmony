"""Streaming file downloader for arbitrary HTTP(S) URLs.

`Downloader` fetches a file from any absolute URL, streaming it to disk via an
atomic temp-then-rename write; an existing file whose byte size already matches
the expected size is left untouched. Bytes are written verbatim — compressed
files stay compressed on disk and are inflated downstream at ingest. It is not
tied to any particular host or data provider.
"""

import asyncio
import os
import random
from pathlib import Path
from typing import Final, Self

import httpx

_CHUNK_SIZE: Final = 1 << 20
_RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})


class SizeMismatchError(RuntimeError):
    def __init__(self, url: str, expected: int, actual: int) -> None:
        super().__init__(f"{url}: expected {expected} bytes, got {actual}")
        self.url = url
        self.expected = expected
        self.actual = actual


class Downloader:
    def __init__(
        self,
        *,
        max_concurrent: int = 3,
        timeout: httpx.Timeout = httpx.Timeout(10.0, read=None),
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            limits=httpx.Limits(max_connections=max_concurrent),
        )
        self._sem = asyncio.Semaphore(max_concurrent)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap

    async def download(
        self, url: str, dest: Path, *, expected_size: int | None = None
    ) -> Path:
        if dest.exists() and (expected_size is None or dest.stat().st_size == expected_size):
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        for attempt in range(self._max_retries + 1):
            last = attempt == self._max_retries
            try:
                size = await self._stream_to(url, tmp)
            except (httpx.TransportError, httpx.TimeoutException):
                if last:
                    raise
            except httpx.HTTPStatusError as exc:
                if last or exc.response.status_code not in _RETRYABLE_STATUS:
                    raise
            else:
                if expected_size is not None and size != expected_size:
                    tmp.unlink(missing_ok=True)
                    raise SizeMismatchError(url, expected_size, size)
                os.replace(tmp, dest)
                return dest
            await asyncio.sleep(self._backoff(attempt))
        raise AssertionError("retry loop exited without returning")

    async def _stream_to(self, url: str, tmp: Path) -> int:
        async with self._sem, self._client.stream("GET", url) as response:
            if response.is_error:
                await response.aread()
            response.raise_for_status()
            written = 0
            with tmp.open("wb") as f:
                async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                    written += f.write(chunk)
        return written

    def _backoff(self, attempt: int) -> float:
        return random.uniform(0.0, min(self._backoff_cap, self._backoff_base * 2**attempt))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()
