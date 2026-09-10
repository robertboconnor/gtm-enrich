"""Fetch orchestration: robots, candidate URLs, caching, and page assembly.

Deliberately backend-agnostic. Whether the bytes came from httpx, Firecrawl,
crawl4ai, or an Apify Actor, the same robots check runs first, the same apex/www
fallback applies, the same cache is consulted, and the same `ScrapedPage` comes
out the other end.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from ..config import ScrapeSettings
from ..models import ScrapedPage, utcnow
from .fetchers import Fetcher, FetcherError, build_fetcher
from .markdown import (
    content_hash,
    detect_tech,
    extract_links,
    extract_links_from_markdown,
    html_to_markdown,
)

log = logging.getLogger(__name__)

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
MIN_USEFUL_MARKDOWN = 200


class FetchError(RuntimeError):
    """A homepage could not be retrieved. Carries a human-readable reason."""


def normalize_domain(raw: str) -> str:
    """'https://WWW.Example.com/pricing?x=1' -> 'example.com'."""
    value = raw.strip().lower()
    if not value:
        raise ValueError("Empty domain.")
    if _SCHEME_RE.match(value):
        value = urlparse(value).netloc
    value = value.split("/")[0].split("@")[-1]
    if value.startswith("www."):
        value = value[4:]
    if ":" in value:
        value = value.split(":")[0]
    if "." not in value:
        raise ValueError(f"Not a domain: {raw!r}")
    return value


def candidate_urls(domain: str) -> list[str]:
    """Try apex over https first, then www -- covers most redirect setups."""
    return [f"https://{domain}/", f"https://www.{domain}/"]


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #


class RobotsCache:
    """One robots.txt fetch per host per run, shared across coroutines.

    Checked for every backend, not just the direct one. A hosted scraper has its
    own compliance story, but the decision to request a page is ours.
    """

    def __init__(self, client: httpx.AsyncClient, user_agent: str) -> None:
        self._client = client
        self._user_agent = user_agent
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def allowed(self, url: str) -> bool:
        host = urlparse(url).netloc
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            if host not in self._parsers:
                self._parsers[host] = await self._load(url)
        parser = self._parsers[host]
        if parser is None:
            return True
        return parser.can_fetch(self._user_agent, url)

    async def _load(self, url: str) -> RobotFileParser | None:
        parts = urlparse(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            resp = await self._client.get(robots_url, timeout=10.0)
        except httpx.HTTPError as exc:
            log.debug("robots.txt unreachable for %s: %s", parts.netloc, exc)
            return None
        if resp.status_code >= 400:
            return None
        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #


def _cache_path(cache_dir: Path, domain: str) -> Path:
    safe = re.sub(r"[^a-z0-9.-]", "_", domain)
    return cache_dir / f"{safe}.json"


def read_cache(settings: ScrapeSettings, domain: str) -> ScrapedPage | None:
    path = _cache_path(settings.cache_dir, domain)
    if not path.is_file():
        return None
    try:
        page = ScrapedPage.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        log.debug("Ignoring unreadable cache entry %s: %s", path, exc)
        return None
    age_hours = (utcnow() - page.fetched_at).total_seconds() / 3600
    if age_hours > settings.cache_ttl_hours:
        return None
    page.from_cache = True
    return page


def write_cache(settings: ScrapeSettings, page: ScrapedPage) -> None:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(settings.cache_dir, page.domain)
    path.write_text(page.model_dump_json(indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# page assembly
# --------------------------------------------------------------------------- #


def build_page(domain: str, content, settings: ScrapeSettings) -> ScrapedPage:
    """Turn whatever a backend returned into a `ScrapedPage`.

    Backends that render their own markdown are trusted for it; the HTML, when
    present, is still parsed for links and vendor fingerprints. A backend that
    returns markdown only degrades gracefully -- links come from the markdown
    and `tech_signals` is empty, because the tags it reads no longer exist.
    """
    html = content.html
    title = content.title
    description = content.description
    markdown = content.markdown

    if html:
        converted, meta = html_to_markdown(html, content.final_url, settings.max_markdown_chars)
        markdown = markdown or converted
        title = title or meta["title"]
        description = description or meta["meta_description"]

        from bs4 import BeautifulSoup  # local import keeps module import cheap

        soup = BeautifulSoup(html, "html.parser")
        links = extract_links(soup, content.final_url)
        tech_signals = detect_tech(html, content.final_url)
    else:
        links = extract_links_from_markdown(markdown or "", content.final_url)
        tech_signals = []

    markdown = (markdown or "").strip()
    if len(markdown) > settings.max_markdown_chars:
        markdown = markdown[: settings.max_markdown_chars].rsplit("\n", 1)[0] + "\n\n_[truncated]_"

    return ScrapedPage(
        domain=domain,
        final_url=content.final_url,
        status_code=content.status_code,
        fetched_at=utcnow(),
        title=title,
        meta_description=description,
        markdown=markdown,
        links=links,
        tech_signals=tech_signals,
        content_hash=content_hash(markdown),
    )


async def scrape_domain(
    client: httpx.AsyncClient,
    robots: RobotsCache,
    domain: str,
    settings: ScrapeSettings,
    fetcher: Fetcher | None = None,
    *,
    use_cache: bool = True,
) -> ScrapedPage:
    """Fetch one homepage and render it. Raises `FetchError` on failure."""
    if use_cache:
        cached = read_cache(settings, domain)
        if cached is not None:
            log.debug("cache hit for %s", domain)
            return cached

    fetcher = fetcher or build_fetcher(settings, client)

    errors: list[str] = []
    for url in candidate_urls(domain):
        if settings.respect_robots and not await robots.allowed(url):
            errors.append(f"{url}: disallowed by robots.txt")
            continue
        try:
            content = await fetcher.fetch(url)
        except FetcherError as exc:
            errors.append(str(exc))
            continue

        page = build_page(domain, content, settings)
        if len(page.markdown) < MIN_USEFUL_MARKDOWN:
            suffix = (
                ""
                if fetcher.renders_javascript
                else " — try --scraper firecrawl, crawl4ai, or apify, which render JavaScript"
            )
            errors.append(
                f"{url}: page rendered to {len(page.markdown)} chars (likely JS-only){suffix}"
            )
            continue

        write_cache(settings, page)
        return page

    raise FetchError("; ".join(errors) or f"{domain}: no candidate URL succeeded")


def build_client(settings: ScrapeSettings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=settings.max_redirects,
        timeout=settings.timeout_seconds,
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


async def scrape_many(
    domains: list[str],
    settings: ScrapeSettings,
    *,
    use_cache: bool = True,
    on_result: object = None,
) -> dict[str, ScrapedPage | FetchError]:
    """Scrape a list of domains with bounded concurrency.

    Returns a dict keyed by domain; each value is either a page or the error
    that stopped it, so a partial failure never sinks the whole run.
    """
    results: dict[str, ScrapedPage | FetchError] = {}
    semaphore = asyncio.Semaphore(settings.concurrency)

    async with build_client(settings) as client:
        robots = RobotsCache(client, settings.user_agent)
        try:
            fetcher = build_fetcher(settings, client)
        except FetcherError as exc:
            # A misconfigured backend is a whole-run problem, not a per-row one.
            return {d: FetchError(str(exc)) for d in domains}

        async def one(domain: str) -> None:
            async with semaphore:
                try:
                    results[domain] = await scrape_domain(
                        client, robots, domain, settings, fetcher, use_cache=use_cache
                    )
                except (FetchError, FetcherError, ValueError) as exc:
                    results[domain] = FetchError(str(exc))
                if callable(on_result):
                    on_result(domain, results[domain])

        try:
            await asyncio.gather(*(one(d) for d in domains))
        finally:
            await fetcher.aclose()

    return results
