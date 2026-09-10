"""Apify: run a crawler Actor and take the dataset item back synchronously.

`POST /v2/acts/{actorId}/run-sync-get-dataset-items` starts the Actor, waits,
and returns its dataset rows in the same call -- no polling loop. The default
Actor is `apify~website-content-crawler`, but the Actor id and its input are
configurable, because Apify's value here is the marketplace: swapping in a
different crawler should not mean editing Python.

The token goes in an Authorization header rather than the `?token=` query
parameter Apify's examples use, so it stays out of URLs, logs, and proxies.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .base import FetchedContent, Fetcher, FetcherError

API_ROOT = "https://api.apify.com/v2"

# Field names vary by Actor, so read the first one that shows up.
_MARKDOWN_KEYS = ("markdown", "text")
_HTML_KEYS = ("html", "rawHtml", "body")


class ApifyFetcher(Fetcher):
    name = "apify"
    renders_javascript = True

    def __init__(
        self,
        actor_id: str = "apify~website-content-crawler",
        api_token: str | None = None,
        timeout: float = 180.0,
        client: httpx.AsyncClient | None = None,
        actor_input: dict[str, Any] | None = None,
    ) -> None:
        self.actor_id = actor_id
        self._token = api_token or self.require_env("APIFY_API_TOKEN", self.name)
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._extra_input = actor_input or {}
        # Actor-run timeout, in seconds, as Apify wants it.
        self._run_timeout = int(os.getenv("APIFY_RUN_TIMEOUT", "120"))

    async def fetch(self, url: str) -> FetchedContent:
        endpoint = f"{API_ROOT}/acts/{self.actor_id}/run-sync-get-dataset-items"
        actor_input: dict[str, Any] = {
            "startUrls": [{"url": url}],
            "maxCrawlPages": 1,
            "maxCrawlDepth": 0,
            "saveHtml": True,
            "saveMarkdown": True,
            **self._extra_input,
        }

        try:
            response = await self._client.post(
                endpoint,
                headers={"Authorization": f"Bearer {self._token}"},
                params={"timeout": self._run_timeout, "limit": 1, "format": "json"},
                json=actor_input,
            )
        except httpx.HTTPError as exc:
            raise FetcherError(f"{url}: could not reach Apify ({exc})") from exc

        if response.status_code >= 400:
            raise FetcherError(f"{url}: Apify {response.status_code}: {response.text[:300]}")

        items = response.json()
        if not isinstance(items, list) or not items:
            raise FetcherError(f"{url}: Apify Actor '{self.actor_id}' produced no dataset items.")
        item = items[0]

        markdown = next((item[k] for k in _MARKDOWN_KEYS if item.get(k)), None)
        html = next((item[k] for k in _HTML_KEYS if item.get(k)), None)
        if not markdown and not html:
            raise FetcherError(
                f"{url}: Apify Actor '{self.actor_id}' returned an item with no text or HTML "
                f"(keys: {sorted(item)[:10]})."
            )

        return FetchedContent(
            final_url=item.get("loadedUrl") or item.get("url") or url,
            status_code=int(item.get("statusCode") or 200),
            html=html,
            markdown=markdown,
            title=item.get("title") or (item.get("metadata") or {}).get("title"),
            description=item.get("description")
            or (item.get("metadata") or {}).get("description"),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
