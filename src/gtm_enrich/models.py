"""Typed contracts for every stage of the pipeline.

The whole point of this module is that nothing untyped ever reaches a CRM.
Scrape -> `ScrapedPage`. Analyze -> `HomepageAnalysis` (schema-enforced by the
model, then validated again by pydantic). Map -> `CrmRecord`. Write -> `WriteResult`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Stage 1: scrape
# --------------------------------------------------------------------------- #


class PageLink(BaseModel):
    """A link found on the homepage, normalized to an absolute URL."""

    text: str
    url: str


class ScrapedPage(BaseModel):
    """The raw material: one homepage, rendered to markdown."""

    domain: str
    final_url: str
    status_code: int
    fetched_at: datetime
    title: str | None = None
    meta_description: str | None = None
    markdown: str
    links: list[PageLink] = Field(default_factory=list)
    # Vendor fingerprints read off <script>/<link> hosts, not from the LLM.
    tech_signals: list[str] = Field(default_factory=list)
    content_hash: str
    from_cache: bool = False


# --------------------------------------------------------------------------- #
# Stage 2: analyze
# --------------------------------------------------------------------------- #

Segment = Literal["SMB", "Mid-Market", "Enterprise", "Consumer", "Mixed", "Unclear"]
SellsTo = Literal["B2B", "B2C", "B2B2C", "Unclear"]
BusinessModel = Literal[
    "SaaS",
    "Marketplace",
    "Ecommerce",
    "Services",
    "Hardware",
    "Media",
    "Nonprofit",
    "Other",
    "Unclear",
]


class Evidence(BaseModel):
    """A claim is only as good as the line of page copy behind it."""

    claim: str = Field(description="Which finding this quote supports.")
    quote: str = Field(description="Verbatim snippet from the page, under 200 chars.")


class HomepageAnalysis(BaseModel):
    """The questions we ask of every homepage.

    This doubles as the JSON schema handed to the model, so field descriptions
    are prompt surface area -- edit them with that in mind.
    """

    model_config = ConfigDict(extra="forbid")

    company_name: str = Field(description="Company name as presented on the page.")
    one_liner: str = Field(
        description="What the company does, in one sentence, in your own words. Max 200 chars."
    )
    category: str = Field(
        description="Product/market category, e.g. 'video hosting', 'payroll software'."
    )
    sells_to: SellsTo = Field(description="Who the company sells to.")
    segment: Segment = Field(
        description="Company size band the marketing copy is aimed at, judged by "
        "proof points: named enterprise logos and 'SOC 2'/'SSO' skew Enterprise; "
        "self-serve signup and low published prices skew SMB."
    )
    business_model: BusinessModel = Field(description="How the company makes money.")
    industries_served: list[str] = Field(
        description="Industries or verticals the page explicitly names. Empty if none named.",
        max_length=8,
    )
    icp_fit_score: int = Field(
        ge=0,
        le=100,
        description="0-100 fit against the ICP definition in the system prompt. "
        "Score the evidence on the page, not your prior knowledge of the company.",
    )
    icp_fit_rationale: str = Field(
        description="Two sentences maximum on why that score, citing page evidence."
    )
    buying_signals: list[str] = Field(
        description="Concrete signals a rep could act on: recent funding, hiring, a "
        "new product launch, a named integration, a customer-facing event. "
        "Empty list if the page shows none -- do not invent them.",
        max_length=6,
    )
    disqualifiers: list[str] = Field(
        description="Reasons this account may be a poor fit or unreachable, e.g. "
        "a direct competitor, wrong geography, obviously pre-revenue.",
        max_length=6,
    )
    primary_cta: str = Field(
        description="The main call to action on the page, e.g. 'Book a demo', "
        "'Start free trial'. 'None' if the page has no clear CTA."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Your confidence in this analysis overall. Thin or "
        "under-construction pages should score low.",
    )
    evidence: list[Evidence] = Field(
        description="2-4 quotes from the page backing the most important findings.",
        max_length=4,
    )


class Provenance(BaseModel):
    """Where a record's values came from. Ops teams have to be able to answer this."""

    source_url: str
    scraped_at: datetime
    analyzed_at: datetime
    analyzer: str = Field(description="'llm:<model-id>' or 'heuristic:v1'.")
    content_hash: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class EnrichmentResult(BaseModel):
    """One domain, fully processed. This is what gets persisted and mapped."""

    domain: str
    ok: bool
    analysis: HomepageAnalysis | None = None
    provenance: Provenance | None = None
    tech_signals: list[str] = Field(default_factory=list)
    page_signals: dict[str, bool] = Field(default_factory=dict)
    error: str | None = None

    @property
    def summary_line(self) -> str:
        if not self.ok or self.analysis is None:
            return f"{self.domain}: FAILED ({self.error})"
        a = self.analysis
        return f"{self.domain}: {a.company_name} — {a.category} — ICP {a.icp_fit_score}"


# --------------------------------------------------------------------------- #
# Stage 3/4: map and write
# --------------------------------------------------------------------------- #


class CrmRecord(BaseModel):
    """Destination-shaped payload: API field names, destination-native values."""

    object_type: str = Field(description="e.g. 'Company', 'Account'.")
    match_key: str = Field(description="Field used to find an existing record.")
    match_value: str
    properties: dict[str, object]


class WriteResult(BaseModel):
    domain: str
    destination: str
    action: Literal["created", "updated", "skipped", "failed", "would_create", "would_update"]
    record_id: str | None = None
    changed_fields: list[str] = Field(default_factory=list)
    error: str | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
