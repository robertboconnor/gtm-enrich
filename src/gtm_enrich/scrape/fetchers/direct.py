"""The zero-dependency backend: plain HTTP with httpx.

Free, fast, and correct for server-rendered pages, which is still most marketing
sites. It cannot execute JavaScript -- a React shell comes back as an empty
`<div id="root">` -- so `scrape/fetch.py` detects that case and says which
backend to switch to rather than silently analyzing a blank page.
"""

from __future__ import annotations

import asyncio

import httpx

from .base import FetchedContent, Fetcher, FetcherError

_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class DirectFetcher(Fetcher):
    name = "direct"
    renders_javascript = False

    def __init__(self, client: httpx.AsyncClient, attempts: int = 3) -> None:
        self._client = client
        self._attempts = attempts
        # The client is shared with the robots checker and owned by the caller,
        # so this backend deliberately does not close it.
        self._owns_client = False

    async def fetch(self, url: str) -> FetchedContent:
        response = await self._get_with_retries(url)
        if response.status_code >= 400:
            raise FetcherError(f"{url}: HTTP {response.status_code}")

        content_type = response.headers.get("content-type", "")
        if "html" not in content_type.lower():
            raise FetcherError(f"{url}: unexpected content-type {content_type!r}")

        return FetchedContent(
            final_url=str(response.url),
            status_code=response.status_code,
            html=response.text,
        )

    async def _get_with_retries(self, url: str) -> httpx.Response:
        last: Exception | str | None = None
        for attempt in range(self._attempts):
            try:
                response = await self._client.get(url)
            except httpx.HTTPError as exc:
                last = exc
            else:
                if response.status_code not in _RETRY_STATUS:
                    return response
                last = f"HTTP {response.status_code}"
            if attempt < self._attempts - 1:
                await asyncio.sleep(2**attempt)
        raise FetcherError(f"{url}: {last}")
