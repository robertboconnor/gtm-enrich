"""Fetch behaviour against a mocked transport: robots, retries, redirects, cache."""

from __future__ import annotations

import httpx
import pytest

from gtm_enrich.config import ScrapeSettings
from gtm_enrich.scrape.fetch import (
    FetchError,
    RobotsCache,
    read_cache,
    scrape_domain,
)

ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"
ROBOTS_DENY = "User-agent: *\nDisallow: /\n"


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        headers={"User-Agent": "gtm-enrich-test"},
    )


@pytest.fixture
def scrape_settings(tmp_path) -> ScrapeSettings:
    return ScrapeSettings(cache_dir=tmp_path / "pages", respect_robots=True)


async def test_scrape_writes_cache_and_second_read_hits_it(sample_html, scrape_settings):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        calls["n"] += 1
        return httpx.Response(200, text=sample_html, headers={"content-type": "text/html"})

    async with make_client(handler) as client:
        robots = RobotsCache(client, scrape_settings.user_agent)
        page = await scrape_domain(client, robots, "acme.example", scrape_settings)

    assert page.status_code == 200
    assert calls["n"] == 1
    assert "HubSpot" in page.tech_signals

    cached = read_cache(scrape_settings, "acme.example")
    assert cached is not None
    assert cached.from_cache is True
    assert cached.content_hash == page.content_hash


async def test_robots_disallow_blocks_the_fetch(sample_html, scrape_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_DENY)
        raise AssertionError("must not fetch a disallowed page")

    async with make_client(handler) as client:
        robots = RobotsCache(client, scrape_settings.user_agent)
        with pytest.raises(FetchError, match="robots.txt"):
            await scrape_domain(client, robots, "acme.example", scrape_settings)


async def test_missing_robots_is_treated_as_allowed(sample_html, scrape_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=sample_html, headers={"content-type": "text/html"})

    async with make_client(handler) as client:
        robots = RobotsCache(client, scrape_settings.user_agent)
        page = await scrape_domain(client, robots, "acme.example", scrape_settings)
    assert page.status_code == 200


async def test_apex_failure_falls_back_to_www(sample_html, scrape_settings):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        seen.append(request.url.host)
        if request.url.host == "acme.example":
            return httpx.Response(404)
        return httpx.Response(200, text=sample_html, headers={"content-type": "text/html"})

    async with make_client(handler) as client:
        robots = RobotsCache(client, scrape_settings.user_agent)
        page = await scrape_domain(client, robots, "acme.example", scrape_settings)

    assert seen == ["acme.example", "www.acme.example"]
    assert page.final_url.startswith("https://www.acme.example")


async def test_non_html_content_type_is_rejected(scrape_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        return httpx.Response(200, text="{}", headers={"content-type": "application/json"})

    async with make_client(handler) as client:
        robots = RobotsCache(client, scrape_settings.user_agent)
        with pytest.raises(FetchError, match="content-type"):
            await scrape_domain(client, robots, "acme.example", scrape_settings)


async def test_javascript_only_page_is_rejected(scrape_settings):
    shell = "<html><head><title>App</title></head><body><div id='root'></div></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        return httpx.Response(200, text=shell, headers={"content-type": "text/html"})

    async with make_client(handler) as client:
        robots = RobotsCache(client, scrape_settings.user_agent)
        with pytest.raises(FetchError, match="JS-only"):
            await scrape_domain(client, robots, "acme.example", scrape_settings)
