"""Fetcher registry. Add a module here and `--scraper <name>` works.

`direct` needs no credentials and is the default; the other three render
JavaScript and each need a key or a running server.
"""

from __future__ import annotations

import httpx

from ...config import ScrapeSettings
from .apify import ApifyFetcher
from .base import FetchedContent, Fetcher, FetcherError, FetcherNotConfigured
from .crawl4ai import Crawl4aiFetcher
from .direct import DirectFetcher
from .firecrawl import FirecrawlFetcher

SCRAPERS = ("direct", "firecrawl", "crawl4ai", "apify")

# What each backend needs before it will run. Surfaced by `gtm-enrich check`.
SCRAPER_REQUIREMENTS: dict[str, str] = {
    "direct": "nothing — built in",
    "firecrawl": "FIRECRAWL_API_KEY",
    "crawl4ai": "a running crawl4ai server (CRAWL4AI_API_URL)",
    "apify": "APIFY_API_TOKEN",
}


def build_fetcher(settings: ScrapeSettings, client: httpx.AsyncClient) -> Fetcher:
    """Instantiate the configured backend.

    `client` is the shared httpx client used for robots.txt; only the `direct`
    backend reuses it, since the others talk to their own API host.
    """
    backend = settings.backend
    if backend == "direct":
        return DirectFetcher(client)
    if backend == "firecrawl":
        return FirecrawlFetcher(
            api_url=settings.firecrawl_api_url, timeout=settings.timeout_seconds * 4
        )
    if backend == "crawl4ai":
        return Crawl4aiFetcher(
            api_url=settings.crawl4ai_api_url, timeout=settings.timeout_seconds * 6
        )
    if backend == "apify":
        return ApifyFetcher(
            actor_id=settings.apify_actor_id, timeout=settings.timeout_seconds * 9
        )
    raise FetcherNotConfigured(
        f"Unknown scraper backend '{backend}'. Known: {', '.join(SCRAPERS)}"
    )


__all__ = [
    "ApifyFetcher",
    "Crawl4aiFetcher",
    "DirectFetcher",
    "FetchedContent",
    "Fetcher",
    "FetcherError",
    "FetcherNotConfigured",
    "FirecrawlFetcher",
    "SCRAPERS",
    "SCRAPER_REQUIREMENTS",
    "build_fetcher",
]
