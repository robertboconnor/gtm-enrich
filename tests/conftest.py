from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from gtm_enrich.config import IcpProfile, MappingConfig, Settings
from gtm_enrich.models import (
    EnrichmentResult,
    Evidence,
    HomepageAnalysis,
    Provenance,
    ScrapedPage,
)
from gtm_enrich.scrape.markdown import (
    content_hash,
    detect_page_signals,
    detect_tech,
    html_to_markdown,
)

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sample_html() -> str:
    return (FIXTURES / "sample_homepage.html").read_text(encoding="utf-8")


@pytest.fixture
def sample_page(sample_html: str) -> ScrapedPage:
    from bs4 import BeautifulSoup

    from gtm_enrich.scrape.markdown import extract_links

    url = "https://acme.example/"
    md, meta = html_to_markdown(sample_html, url)
    return ScrapedPage(
        domain="acme.example",
        final_url=url,
        status_code=200,
        fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        title=meta["title"],
        meta_description=meta["meta_description"],
        markdown=md,
        links=extract_links(BeautifulSoup(sample_html, "html.parser"), url),
        tech_signals=detect_tech(sample_html, url),
        content_hash=content_hash(md),
    )


@pytest.fixture
def page_signals(sample_page: ScrapedPage) -> dict[str, bool]:
    return detect_page_signals(sample_page.links, sample_page.markdown)


@pytest.fixture
def icp() -> IcpProfile:
    return IcpProfile.load(REPO_ROOT / "config" / "icp.yaml")


@pytest.fixture
def mapping() -> MappingConfig:
    return MappingConfig.load(REPO_ROOT / "config" / "mapping.yaml")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings.from_env()
    object.__setattr__(s.scrape, "cache_dir", tmp_path / "pages")
    s.output_dir = tmp_path / "out"
    return s


@pytest.fixture
def analysis() -> HomepageAnalysis:
    return HomepageAnalysis(
        company_name="Acme Analytics",
        one_liner="Product analytics for B2B SaaS marketing teams.",
        category="analytics",
        sells_to="B2B",
        segment="Mid-Market",
        business_model="SaaS",
        industries_served=["software"],
        icp_fit_score=78,
        icp_fit_rationale="Runs content marketing and shows a demo CTA.",
        buying_signals=["Careers page is live (hiring)"],
        disqualifiers=[],
        primary_cta="Book a demo",
        confidence=0.82,
        evidence=[Evidence(claim="category", quote="Product analytics built for B2B SaaS")],
    )


@pytest.fixture
def enrichment(analysis: HomepageAnalysis, sample_page: ScrapedPage) -> EnrichmentResult:
    return EnrichmentResult(
        domain="acme.example",
        ok=True,
        analysis=analysis,
        provenance=Provenance(
            source_url=sample_page.final_url,
            scraped_at=sample_page.fetched_at,
            analyzed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            analyzer="llm:claude-opus-5",
            content_hash=sample_page.content_hash,
            input_tokens=4000,
            output_tokens=500,
            cost_usd=0.0325,
        ),
        tech_signals=sample_page.tech_signals,
        page_signals={"has_pricing_page": True, "has_demo_cta": True, "has_careers_page": True},
    )
