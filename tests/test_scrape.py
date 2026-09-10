"""Scraping: URL normalization, markdown extraction, and the signals we read off HTML."""

from __future__ import annotations

import pytest

from gtm_enrich.scrape.fetch import candidate_urls, normalize_domain
from gtm_enrich.scrape.markdown import (
    content_hash,
    detect_page_signals,
    detect_tech,
    html_to_markdown,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "example.com"),
        ("https://www.Example.com/pricing?utm=1", "example.com"),
        ("HTTP://EXAMPLE.COM", "example.com"),
        ("  www.example.co.uk  ", "example.co.uk"),
        ("example.com:8080", "example.com"),
        ("https://sub.example.com/", "sub.example.com"),
    ],
)
def test_normalize_domain(raw: str, expected: str) -> None:
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "notadomain", "http://"])
def test_normalize_domain_rejects_junk(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_domain(raw)


def test_candidate_urls_tries_apex_then_www() -> None:
    assert candidate_urls("example.com") == [
        "https://example.com/",
        "https://www.example.com/",
    ]


def test_html_to_markdown_extracts_metadata(sample_html: str) -> None:
    md, meta = html_to_markdown(sample_html, "https://acme.example/")
    assert meta["title"] == "Acme Analytics | Product Analytics for B2B SaaS Teams"
    assert "which content drives pipeline" in (meta["meta_description"] or "")
    assert "Product analytics built for B2B SaaS marketing teams" in md


def test_html_to_markdown_strips_scripts_and_styles(sample_html: str) -> None:
    md, _ = html_to_markdown(sample_html, "https://acme.example/")
    assert "window.junk" not in md
    assert "color:red" not in md


def test_html_to_markdown_truncates(sample_html: str) -> None:
    md, _ = html_to_markdown(sample_html, "https://acme.example/", max_chars=120)
    assert md.endswith("_[truncated]_")
    assert len(md) < 300


def test_detect_tech_reads_asset_hosts(sample_html: str) -> None:
    found = detect_tech(sample_html, "https://acme.example/")
    assert "HubSpot" in found
    assert "Segment" in found
    assert "WordPress" in found  # relative /wp-content/ path, resolved against the base URL
    assert "Marketo" not in found


def test_page_signals_from_links(sample_page) -> None:
    signals = detect_page_signals(sample_page.links, sample_page.markdown)
    assert signals["has_pricing_page"]
    assert signals["has_case_studies"]
    assert signals["has_careers_page"]  # greenhouse.io link, not a /careers path
    assert signals["has_demo_cta"]
    assert signals["has_login"]
    assert not signals["has_integrations"]


def test_links_skip_anchors_and_mailto(sample_page) -> None:
    urls = [link.url for link in sample_page.links]
    assert not any(u.startswith("mailto:") for u in urls)
    assert not any(u.endswith("#") for u in urls)
    assert "https://acme.example/pricing" in urls


def test_content_hash_is_stable_and_sensitive() -> None:
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")
