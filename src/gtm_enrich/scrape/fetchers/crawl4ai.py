"""crawl4ai: the self-hosted option.

Talks to a crawl4ai Docker server (`POST http://localhost:11235/crawl`), so
nothing leaves your network and there is no per-page cost -- the tradeoff is
that you run the container. Auth is optional and only needed when the server has
JWT enabled.

The server has returned two response shapes across versions -- a bare result and
a `{"results": [...]}` envelope -- so this reads either.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .base import FetchedContent, Fetcher, FetcherError


class Crawl4aiFetcher(Fetcher):
    name = "crawl4ai"
    renders_javascript = True

    def __init__(
        self,
        api_url: str = "http://localhost:11235/crawl",
        api_token: str | None = None,
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_url = api_url
        # Optional: only set when the server runs with jwt_enabled.
        self._token = api_token or os.getenv("CRAWL4AI_API_TOKEN")
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def fetch(self, url: str) -> FetchedContent:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            response = await self._client.post(
                self.api_url,
                headers=headers,
                json={
                    "urls": [url],
                    "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
                    "crawler_config": {
                        "type": "CrawlerRunConfig",
                        "params": {"cache_mode": "bypass"},
                    },
                },
            )
        except httpx.HTTPError as exc:
            raise FetcherError(
                f"{url}: could not reach crawl4ai at {self.api_url} ({exc}). "
                "Is the Docker container running?"
            ) from exc

        if response.status_code >= 400:
            raise FetcherError(f"{url}: crawl4ai {response.status_code}: {response.text[:300]}")

        result = self._unwrap(response.json())
        if not result.get("success", True):
            raise FetcherError(f"{url}: crawl4ai reported failure: {str(result)[:300]}")

        markdown = result.get("markdown")
        if isinstance(markdown, dict):
            # Newer builds return a MarkdownGenerationResult rather than a string.
            markdown = markdown.get("raw_markdown") or markdown.get("fit_markdown")

        html = result.get("raw_html") or result.get("cleaned_html")
        if not markdown and not html:
            raise FetcherError(f"{url}: crawl4ai returned no content.")

        metadata = result.get("metadata") or {}
        return FetchedContent(
            final_url=result.get("url") or url,
            status_code=int(result.get("status_code") or 200),
            html=html,
            markdown=markdown if isinstance(markdown, str) else None,
            title=metadata.get("title"),
            description=metadata.get("description"),
        )

    @staticmethod
    def _unwrap(payload: Any) -> dict:
        if isinstance(payload, dict) and isinstance(payload.get("results"), list):
            results = payload["results"]
            if not results:
                raise FetcherError("crawl4ai returned an empty results array.")
            return results[0]
        if isinstance(payload, list):
            if not payload:
                raise FetcherError("crawl4ai returned an empty array.")
            return payload[0]
        if isinstance(payload, dict):
            return payload
        raise FetcherError(f"Unexpected crawl4ai response type: {type(payload).__name__}")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
