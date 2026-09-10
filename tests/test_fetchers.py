"""Fetch backends: each against a mock transport, plus the contract they share.

None of these tests reach the network. What they pin down is the part that
actually breaks in production: that four very different response shapes all
normalize to the same `FetchedContent`, and that a backend returning markdown
without HTML degrades in a defined way instead of crashing.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gtm_enrich.config import ScrapeSettings
from gtm_enrich.scrape.fetch import build_page
from gtm_enrich.scrape.fetchers import (
    SCRAPER_REQUIREMENTS,
    SCRAPERS,
    ApifyFetcher,
    Crawl4aiFetcher,
    DirectFetcher,
    FetchedContent,
    FetcherError,
    FetcherNotConfigured,
    FirecrawlFetcher,
    build_fetcher,
)

MARKDOWN = "# Acme\n\nProduct analytics for [B2B SaaS](https://acme.example/product) teams.\n"


def mock_client(handler, **kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)


# --------------------------------------------------------------------------- #
# the shared contract
# --------------------------------------------------------------------------- #


def test_every_backend_is_registered_with_a_stated_requirement() -> None:
    assert set(SCRAPERS) == set(SCRAPER_REQUIREMENTS)


def test_content_with_neither_html_nor_markdown_is_rejected() -> None:
    with pytest.raises(FetcherError, match="neither HTML nor markdown"):
        FetchedContent(final_url="https://acme.example/", status_code=200)


def test_unknown_backend_is_a_clean_error() -> None:
    settings = ScrapeSettings(backend="scrapy")
    with pytest.raises(FetcherNotConfigured, match="Unknown scraper backend"):
        build_fetcher(settings, httpx.AsyncClient())


def test_only_direct_needs_no_credentials(monkeypatch) -> None:
    for var in ("FIRECRAWL_API_KEY", "APIFY_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    for backend in ("firecrawl", "apify"):
        with pytest.raises(FetcherNotConfigured, match="--scraper direct"):
            build_fetcher(ScrapeSettings(backend=backend), httpx.AsyncClient())


# --------------------------------------------------------------------------- #
# direct
# --------------------------------------------------------------------------- #


async def test_direct_returns_html(sample_html) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sample_html, headers={"content-type": "text/html"})

    async with mock_client(handler) as client:
        content = await DirectFetcher(client).fetch("https://acme.example/")
    assert content.status_code == 200
    assert content.markdown is None  # direct never renders markdown itself
    assert "Acme Analytics" in content.html


async def test_direct_retries_5xx_then_succeeds(sample_html, monkeypatch) -> None:
    monkeypatch.setattr("gtm_enrich.scrape.fetchers.direct.asyncio.sleep", _no_sleep)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, text=sample_html, headers={"content-type": "text/html"})

    async with mock_client(handler) as client:
        content = await DirectFetcher(client).fetch("https://acme.example/")
    assert attempts["n"] == 3
    assert content.status_code == 200


async def _no_sleep(seconds: float) -> None:
    return None


# --------------------------------------------------------------------------- #
# firecrawl
# --------------------------------------------------------------------------- #


def firecrawl_payload(**overrides) -> dict:
    data = {
        "markdown": MARKDOWN,
        # Firecrawl returns both when asked; `html` is cleaned (no <script>),
        # `rawHtml` is the original. Mirrors a real response.
        "html": "<html><body><h1>Acme</h1></body></html>",
        "rawHtml": '<html><head><script src="https://js.hs-scripts.com/1.js">'
                   "</script></head><body><h1>Acme</h1></body></html>",
        "metadata": {
            "title": "Acme Analytics",
            "description": "Analytics for B2B SaaS.",
            "statusCode": 200,
            "sourceURL": "https://acme.example/",
            "url": "https://www.acme.example/",
        },
    }
    data.update(overrides)
    return {"success": True, "data": data}


async def test_firecrawl_normalizes_a_successful_scrape() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = httpx.Request("POST", request.url, content=request.content).content
        return httpx.Response(200, json=firecrawl_payload())

    async with mock_client(handler) as client:
        fetcher = FirecrawlFetcher(api_key="fc-test", client=client)
        content = await fetcher.fetch("https://acme.example/")

    assert seen["auth"] == "Bearer fc-test"
    # onlyMainContent must stay off: the nav is where pricing/careers links live.
    assert b'"onlyMainContent": false' in seen["body"] or b'"onlyMainContent":false' in seen["body"]
    assert b"markdown" in seen["body"] and b"rawHtml" in seen["body"]
    assert content.final_url == "https://www.acme.example/"  # post-redirect URL wins
    assert content.title == "Acme Analytics"
    assert content.markdown == MARKDOWN


async def test_firecrawl_asks_for_rawhtml_not_cleaned_html() -> None:
    """Regression: `html` is cleaned and has no <script> tags, so vendor
    fingerprinting silently found nothing on every account. Measured live:
    the cleaned format had 0 script tags where rawHtml had 44."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["formats"] = json.loads(request.content)["formats"]
        return httpx.Response(200, json=firecrawl_payload())

    async with mock_client(handler) as client:
        content = await FirecrawlFetcher(api_key="k", client=client).fetch("https://acme.example/")

    assert "rawHtml" in seen["formats"]
    assert "js.hs-scripts.com" in content.html  # the fingerprint survived


async def test_firecrawl_falls_back_to_cleaned_html_when_rawhtml_is_absent() -> None:
    payload = firecrawl_payload()
    del payload["data"]["rawHtml"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with mock_client(handler) as client:
        content = await FirecrawlFetcher(api_key="k", client=client).fetch("https://acme.example/")
    assert "<h1>Acme</h1>" in content.html


async def test_firecrawl_handles_list_valued_metadata() -> None:
    """Firecrawl returns title/description as a string or a list, depending on the page."""
    payload = firecrawl_payload()
    payload["data"]["metadata"]["title"] = ["Acme Analytics", "duplicate og:title"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with mock_client(handler) as client:
        content = await FirecrawlFetcher(api_key="k", client=client).fetch("https://acme.example/")
    assert content.title == "Acme Analytics"


async def test_firecrawl_surfaces_api_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text="Insufficient credits")

    async with mock_client(handler) as client:
        with pytest.raises(FetcherError, match="Firecrawl 402"):
            await FirecrawlFetcher(api_key="k", client=client).fetch("https://acme.example/")


async def test_firecrawl_treats_success_false_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "error": "blocked"})

    async with mock_client(handler) as client:
        with pytest.raises(FetcherError, match="reported failure"):
            await FirecrawlFetcher(api_key="k", client=client).fetch("https://acme.example/")


# --------------------------------------------------------------------------- #
# crawl4ai
# --------------------------------------------------------------------------- #


def crawl4ai_result(**overrides) -> dict:
    result = {
        "success": True,
        "status_code": 200,
        "url": "https://acme.example/",
        "markdown": MARKDOWN,
        "raw_html": "<html><body><h1>Acme</h1></body></html>",
        "metadata": {"title": "Acme Analytics", "description": "Analytics."},
    }
    result.update(overrides)
    return result


@pytest.mark.parametrize(
    "envelope",
    [
        lambda r: {"success": True, "results": [r]},  # documented shape
        lambda r: r,                                   # older bare-result shape
        lambda r: [r],                                 # bare array
    ],
)
async def test_crawl4ai_reads_every_response_envelope(envelope) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope(crawl4ai_result()))

    async with mock_client(handler) as client:
        content = await Crawl4aiFetcher(client=client).fetch("https://acme.example/")
    assert content.markdown == MARKDOWN
    assert content.title == "Acme Analytics"


async def test_crawl4ai_unwraps_markdown_generation_result() -> None:
    """Newer builds return an object here instead of a string."""
    result = crawl4ai_result(markdown={"raw_markdown": MARKDOWN, "fit_markdown": "short"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [result]})

    async with mock_client(handler) as client:
        content = await Crawl4aiFetcher(client=client).fetch("https://acme.example/")
    assert content.markdown == MARKDOWN


async def test_crawl4ai_sends_bearer_only_when_a_token_is_set(monkeypatch) -> None:
    monkeypatch.delenv("CRAWL4AI_API_TOKEN", raising=False)
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"results": [crawl4ai_result()]})

    async with mock_client(handler) as client:
        await Crawl4aiFetcher(client=client).fetch("https://acme.example/")
        await Crawl4aiFetcher(api_token="jwt", client=client).fetch("https://acme.example/")
    assert seen == [None, "Bearer jwt"]


async def test_crawl4ai_connection_failure_names_the_container() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with mock_client(handler) as client:
        with pytest.raises(FetcherError, match="Is the Docker container running"):
            await Crawl4aiFetcher(client=client).fetch("https://acme.example/")


# --------------------------------------------------------------------------- #
# apify
# --------------------------------------------------------------------------- #


async def test_apify_runs_the_actor_and_reads_the_dataset_item() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["query"] = dict(request.url.params)
        return httpx.Response(
            201,
            json=[
                {
                    "url": "https://acme.example/",
                    "loadedUrl": "https://www.acme.example/",
                    "markdown": MARKDOWN,
                    "html": "<html><body>Acme</body></html>",
                    "metadata": {"title": "Acme Analytics"},
                }
            ],
        )

    async with mock_client(handler) as client:
        content = await ApifyFetcher(api_token="apify-test", client=client).fetch(
            "https://acme.example/"
        )

    assert seen["path"] == "/v2/acts/apify~website-content-crawler/run-sync-get-dataset-items"
    # The token belongs in a header, not in the query string.
    assert seen["auth"] == "Bearer apify-test"
    assert "token" not in seen["query"]
    assert content.final_url == "https://www.acme.example/"
    assert content.title == "Acme Analytics"


async def test_apify_empty_dataset_names_the_actor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=[])

    async with mock_client(handler) as client:
        with pytest.raises(FetcherError, match="produced no dataset items"):
            await ApifyFetcher(api_token="t", client=client).fetch("https://acme.example/")


async def test_apify_item_without_content_reports_the_keys_it_saw() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=[{"url": "https://acme.example/", "screenshotUrl": "x"}])

    async with mock_client(handler) as client:
        with pytest.raises(FetcherError, match="screenshotUrl"):
            await ApifyFetcher(api_token="t", client=client).fetch("https://acme.example/")


async def test_apify_actor_id_is_configurable() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(201, json=[{"url": "u", "text": MARKDOWN}])

    async with mock_client(handler) as client:
        await ApifyFetcher(actor_id="me~my-crawler", api_token="t", client=client).fetch(
            "https://acme.example/"
        )
    assert "me~my-crawler" in seen["path"]


# --------------------------------------------------------------------------- #
# markdown-only degradation
# --------------------------------------------------------------------------- #


def test_markdown_only_backend_still_yields_links_but_no_vendors() -> None:
    """A backend that returns no HTML loses vendor detection, and says so by omission."""
    settings = ScrapeSettings()
    content = FetchedContent(
        final_url="https://acme.example/",
        status_code=200,
        markdown=MARKDOWN,
        title="Acme Analytics",
    )
    page = build_page("acme.example", content, settings)

    assert page.markdown == MARKDOWN.strip()
    assert [link.url for link in page.links] == ["https://acme.example/product"]
    assert page.tech_signals == []  # the tags those are read from no longer exist


def test_html_backend_yields_both(sample_html) -> None:
    settings = ScrapeSettings()
    content = FetchedContent(
        final_url="https://acme.example/", status_code=200, html=sample_html
    )
    page = build_page("acme.example", content, settings)

    assert "HubSpot" in page.tech_signals
    assert any(link.url.endswith("/pricing") for link in page.links)


def test_supplied_markdown_wins_over_local_conversion(sample_html) -> None:
    """When a backend renders its own markdown, we trust it and keep the HTML for signals."""
    settings = ScrapeSettings()
    content = FetchedContent(
        final_url="https://acme.example/",
        status_code=200,
        html=sample_html,
        markdown=MARKDOWN,
    )
    page = build_page("acme.example", content, settings)

    assert page.markdown == MARKDOWN.strip()
    assert "HubSpot" in page.tech_signals  # still parsed from the HTML
