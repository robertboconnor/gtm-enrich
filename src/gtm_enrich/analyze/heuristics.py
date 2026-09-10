"""Keyword fallback for when there is no model available.

This is deliberately crude and says so: every result it produces is capped at
low confidence and labelled `heuristic:v1` in provenance, so nobody downstream
mistakes a regex match for judgement. It exists so the pipeline -- and the repo's
test suite -- runs end to end with no API key at all.
"""

from __future__ import annotations

import re

from ..config import IcpProfile
from ..models import Evidence, HomepageAnalysis, Provenance, ScrapedPage, utcnow

MAX_CONFIDENCE = 0.4

_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "video platform": ("video hosting", "video marketing", "webinar", "video platform"),
    "revenue intelligence": ("revenue intelligence", "conversation intelligence", "call recording"),
    "payments": ("payments", "checkout", "billing", "invoicing", "payment processing"),
    "developer tools": ("api", "sdk", "deploy", "developers", "ci/cd", "infrastructure"),
    "marketing automation": ("marketing automation", "email campaigns", "lead nurtur"),
    "crm": ("crm", "pipeline management", "deal management"),
    "analytics": ("analytics", "dashboards", "business intelligence", "data warehouse"),
    "hr / people": ("payroll", "applicant tracking", "onboarding employees", "hris"),
    "security": ("security", "compliance", "soc 2", "vulnerability", "zero trust"),
    "ecommerce": ("online store", "ecommerce", "shopping cart", "dropship"),
    "project management": ("project management", "issue tracking", "roadmap", "sprint"),
    "customer support": ("help desk", "support tickets", "customer support", "live chat"),
}

_VERTICALS = (
    "healthcare", "fintech", "financial services", "education", "retail", "manufacturing",
    "logistics", "real estate", "legal", "insurance", "nonprofit", "government",
    "hospitality", "media", "telecom", "automotive", "energy", "construction",
)

_ENTERPRISE_MARKERS = ("soc 2", "soc2", "sso", "saml", "hipaa", "enterprise-grade",
                       "fortune 500", "procurement", "sla", "dedicated csm", "iso 27001")
_SMB_MARKERS = ("small business", "solopreneur", "freelancer", "get started free",
                "no credit card", "per month", "/mo")
_B2C_MARKERS = ("for you and your family", "shop now", "add to cart", "your order",
                "personal plan", "for individuals")
_B2B_MARKERS = ("for teams", "your team", "businesses", "companies", "organizations",
                "b2b", "revenue teams", "sales teams")

_FUNDING_RE = re.compile(r"\b(series [a-e]|seed round|raised \$\d|funding round)\b", re.I)
_LAUNCH_RE = re.compile(r"\b(introducing|now available|new(?:ly)? launched|announcing)\b", re.I)
_AWARD_RE = re.compile(r"\b(leader in|g2 leader|gartner|forrester wave)\b", re.I)

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+-]*")
_PUNCT_RE = re.compile(r"[^a-z0-9\s]+")

# Words too generic to carry ICP evidence on their own. Without this, a bullet
# like "video hosting platform competitor" matches every page that contains the
# word "platform", and every account scores like a competitor.
_STOPWORDS = {
    "a", "an", "the", "and", "or", "for", "with", "that", "this", "who", "are",
    "not", "but", "has", "have", "their", "them", "they", "from", "any", "all",
    "our", "your", "its", "to", "of", "in", "on", "at", "as", "is", "be", "by",
    "such", "than", "rather", "many", "only", "own", "more", "most", "other",
    "company", "companies", "business", "businesses", "team", "teams", "using",
    "use", "uses", "page", "platform", "product", "products", "customer",
    "customers", "user", "users", "client", "clients", "buyer", "buyers",
    "enough", "already", "great", "run", "runs", "needs", "need",
}


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace -- so bigrams match."""
    return " ".join(_PUNCT_RE.sub(" ", text.lower()).split())


def _bullet_keys(bullet: str) -> list[str]:
    """Distinctive multi-word keys for one ICP bullet.

    Bigrams of adjacent non-stopword terms, e.g. "video hosting" or "marketing
    automation". Bigrams only, deliberately: a single word is never enough
    evidence. "consumer" appears on plenty of B2B homepages; "consumer social"
    does not.
    """
    keys: list[str] = []
    for clause in re.split(r"[,;:]", bullet):
        words = [w for w in _WORD_RE.findall(_normalize(clause)) if w not in _STOPWORDS]
        keys += [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]
    return keys


def _match_bullets(bullets: list[str], corpus: str) -> tuple[int, list[str]]:
    """How many ICP bullets the page supports, and which keys fired.

    Scored per bullet rather than per keyword: one bullet that matches on three
    of its bigrams is still one piece of evidence, not three.
    """
    matched, evidence = 0, []
    for bullet in bullets:
        hits = [k for k in _bullet_keys(bullet) if k in corpus]
        if hits:
            matched += 1
            evidence.append(hits[0])
    return matched, evidence


def _title_to_company(page: ScrapedPage) -> str:
    if page.title:
        # "Wistia | The Video Marketing Platform" -> "Wistia"; also handles "-" and "–".
        parts = re.split(r"\s*[|–—-]\s*", page.title)
        best = min(parts, key=len).strip() if len(parts) > 1 else page.title.strip()
        if 1 < len(best) <= 60:
            return best
    return page.domain.split(".")[0].capitalize()


def _first_paragraph(markdown: str) -> str:
    for block in markdown.split("\n\n"):
        text = re.sub(r"[#*_>\[\]()]", "", block).strip()
        if 40 <= len(text) <= 300 and not text.lower().startswith("http"):
            return text
    return ""


def _score_icp(corpus: str, icp: IcpProfile) -> tuple[int, list[str], list[str]]:
    """Overlap between ICP bullets and page copy. Crude by design.

    Anchored at 50 -- "no idea" -- rather than at zero. A keyword bag can only
    weakly confirm a fit, so the upside is capped well below what a real
    analysis can claim, while an explicit poor-fit match is allowed to bite.
    """
    if icp.heuristic_good or icp.heuristic_poor:
        # Explicit keyword lists from config/icp.yaml -- literal substrings.
        hits = [k for k in icp.heuristic_good if k in corpus]
        misses = [k for k in icp.heuristic_poor if k in corpus]
        good_total, good_matched, poor_matched = len(icp.heuristic_good), len(hits), len(misses)
    else:
        # No keyword list configured: derive bigrams from the prose bullets.
        good_matched, hits = _match_bullets(icp.good_fit, corpus)
        poor_matched, misses = _match_bullets(icp.poor_fit, corpus)
        good_total = len(icp.good_fit)

    if not good_total:
        return 50, hits, misses

    ratio = good_matched / good_total
    score = 50 + round(35 * min(ratio * 1.6, 1.0))  # 50 floor, 85 ceiling
    score -= min(45, 15 * poor_matched)
    return max(0, min(100, int(score))), hits, misses


def analyze_with_heuristics(
    page: ScrapedPage, page_signals: dict[str, bool], icp: IcpProfile
) -> tuple[HomepageAnalysis, Provenance]:
    raw = f"{page.title or ''}\n{page.meta_description or ''}\n{page.markdown}".lower()
    # Bigram keys are matched against punctuation-free text; the marker lists
    # below still want the raw lowercase copy (they contain "/mo", "soc 2").
    corpus = _normalize(raw)

    # Score every category and take the best, rather than the first one that
    # happens to match -- a page mentioning "security" in its footer is not a
    # security product.
    category_scores = {
        cat: sum(raw.count(k) for k in kws) for cat, kws in _CATEGORY_KEYWORDS.items()
    }
    best_category = max(category_scores, key=lambda c: category_scores[c])
    category = best_category if category_scores[best_category] >= 2 else "unknown"

    b2b_hits = sum(m in raw for m in _B2B_MARKERS)
    b2c_hits = sum(m in raw for m in _B2C_MARKERS)
    if b2b_hits > b2c_hits:
        sells_to = "B2B"
    elif b2c_hits > b2b_hits:
        sells_to = "B2C"
    else:
        sells_to = "Unclear"

    ent_hits = sum(m in raw for m in _ENTERPRISE_MARKERS)
    smb_hits = sum(m in raw for m in _SMB_MARKERS)
    if ent_hits >= 2 and ent_hits > smb_hits:
        segment = "Enterprise"
    elif smb_hits >= 2 and smb_hits > ent_hits:
        segment = "SMB"
    elif ent_hits and smb_hits:
        segment = "Mixed"
    else:
        segment = "Unclear"

    if page_signals.get("has_pricing_page") and page_signals.get("has_login"):
        business_model = "SaaS"
    elif "add to cart" in raw or "shopping cart" in raw:
        business_model = "Ecommerce"
    elif any(w in raw for w in ("consulting", "our agency", "professional services")):
        business_model = "Services"
    else:
        business_model = "Unclear"

    industries = [v for v in _VERTICALS if v in raw][:8]

    score, hits, misses = _score_icp(corpus, icp)

    signals: list[str] = []
    if page_signals.get("has_careers_page"):
        signals.append("Careers page is live (hiring)")
    if _FUNDING_RE.search(raw):
        signals.append("Funding mentioned on the homepage")
    if _LAUNCH_RE.search(raw):
        signals.append("Recent product launch language on the homepage")
    if _AWARD_RE.search(raw):
        signals.append("Analyst or review-site recognition claimed")

    disqualifiers: list[str] = []
    if misses:
        disqualifiers.append(f"Poor-fit ICP terms present: {', '.join(misses[:4])}")
    if len(page.markdown) < 800:
        disqualifiers.append("Homepage is very thin; may be a placeholder or JS-only site")

    if page_signals.get("has_demo_cta"):
        cta = "Book a demo"
    elif page_signals.get("has_free_trial"):
        cta = "Start a free trial"
    elif page_signals.get("has_contact_sales"):
        cta = "Contact sales"
    else:
        cta = "None"

    one_liner = (page.meta_description or _first_paragraph(page.markdown) or
                 f"{_title_to_company(page)} homepage.")[:200]

    evidence: list[Evidence] = []
    if page.meta_description:
        evidence.append(Evidence(claim="one_liner", quote=page.meta_description[:200]))
    para = _first_paragraph(page.markdown)
    if para:
        evidence.append(Evidence(claim="category", quote=para[:200]))

    rationale = (
        f"Heuristic keyword match only -- no model was used. "
        f"{len(hits)} good-fit and {len(misses)} poor-fit ICP terms found on the page."
    )

    analysis = HomepageAnalysis(
        company_name=_title_to_company(page),
        one_liner=one_liner,
        category=category,
        sells_to=sells_to,
        segment=segment,
        business_model=business_model,
        industries_served=industries,
        icp_fit_score=score,
        icp_fit_rationale=rationale,
        buying_signals=signals[:6],
        disqualifiers=disqualifiers[:6],
        primary_cta=cta,
        confidence=MAX_CONFIDENCE if len(page.markdown) > 1500 else 0.2,
        evidence=evidence[:4],
    )
    provenance = Provenance(
        source_url=page.final_url,
        scraped_at=page.fetched_at,
        analyzed_at=utcnow(),
        analyzer="heuristic:v1",
        content_hash=page.content_hash,
    )
    return analysis, provenance
