"""Firecrawl: hosted scraping that renders JavaScript and returns markdown.

`POST https://api.firecrawl.dev/v2/scrape` with a Bearer key. We ask for
`markdown` and `rawHtml`: the markdown is better than anything we'd produce
locally, and the raw HTML is what vendor fingerprinting reads.

Two of Firecrawl's defaults are wrong for this job and both are set explicitly:

* `onlyMainContent` is off. Firecrawl's default strips nav and footer, which is
  right for article extraction and wrong here -- the nav is where the pricing,
  product, and careers links live.
* We request `rawHtml`, not `html`. The `html` format is *cleaned*, and cleaning
  removes every `<script>` tag -- which is precisely where `js.hs-scripts.com`,
  `googletagmanager.com`, and every other vendor fingerprint lives. Measured on
  one real homepage: `html` contained 0 script tags, `rawHtml` contained 44.
  Asking for the wrong one silently returns zero detected vendors on every
  account, with no error anywhere.
"""

from __future__ import annotations

import httpx

from .base import FetchedContent, Fetcher, FetcherError


def _first(value: object) -> str | None:
    """Firecrawl returns some metadata fields as either a string or a list."""
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value) if value else None


class FirecrawlFetcher(Fetcher):
    name = "firecrawl"
    renders_javascript = True

    def __init__(
        self,
        api_url: str = "https://api.firecrawl.dev/v2/scrape",
        api_key: str | None = None,
        timeout: float = 90.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_url = api_url
        self._key = api_key or self.require_env("FIRECRAWL_API_KEY", self.name)
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def fetch(self, url: str) -> FetchedContent:
        try:
            response = await self._client.post(
                self.api_url,
                headers={"Authorization": f"Bearer {self._key}"},
                json={
                    "url": url,
                    # rawHtml, not html -- see the module docstring.
                    "formats": ["markdown", "rawHtml"],
                    "onlyMainContent": False,
                },
            )
        except httpx.HTTPError as exc:
            raise FetcherError(f"{url}: could not reach Firecrawl ({exc})") from exc

        if response.status_code >= 400:
            raise FetcherError(f"{url}: Firecrawl {response.status_code}: {response.text[:300]}")

        payload = response.json()
        if not payload.get("success", True):
            raise FetcherError(f"{url}: Firecrawl reported failure: {str(payload)[:300]}")

        data = payload.get("data") or {}
        metadata = data.get("metadata") or {}
        markdown = data.get("markdown")
        # Prefer rawHtml; fall back to the cleaned html so a response that only
        # carries one of them still yields a page (with no vendor signals).
        html = data.get("rawHtml") or data.get("html")
        if not markdown and not html:
            raise FetcherError(f"{url}: Firecrawl returned no content.")

        return FetchedContent(
            # metadata.url is the post-redirect URL; sourceURL is what we asked for.
            final_url=metadata.get("url") or metadata.get("sourceURL") or url,
            status_code=int(metadata.get("statusCode") or response.status_code),
            html=html,
            markdown=markdown,
            title=_first(metadata.get("title")),
            description=_first(metadata.get("description")),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
