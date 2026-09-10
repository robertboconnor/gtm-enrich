"""HTML -> markdown, plus the deterministic signals we can read straight off the DOM.

Two kinds of signal come out of here and it matters which is which:

* `tech_signals` and `page_signals` are *facts* -- a script host is present or it
  isn't, a /pricing link exists or it doesn't. No model involved, no hallucination
  risk, and they stay correct even when the LLM step is skipped entirely.
* The markdown is *input to judgement*, handed to the analysis step.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify

from ..models import PageLink

# Tags that carry no meaning once rendered to markdown.
_NOISE_TAGS = ("script", "style", "noscript", "svg", "iframe", "template", "form")

# host-or-path fragment -> vendor label. Matched against every script/link/img
# URL on the page, so a fragment must be specific enough not to collide.
TECH_SIGNATURES: dict[str, str] = {
    "js.hs-scripts.com": "HubSpot",
    "js.hsforms.net": "HubSpot Forms",
    "hs-analytics.net": "HubSpot",
    "munchkin.marketo.net": "Marketo",
    "pi.pardot.com": "Pardot",
    "cdn.segment.com": "Segment",
    "googletagmanager.com": "Google Tag Manager",
    "google-analytics.com": "Google Analytics",
    "widget.intercom.io": "Intercom",
    "js.driftt.com": "Drift",
    "js.qualified.com": "Qualified",
    "j.6sc.co": "6sense",
    "tag.demandbase.com": "Demandbase",
    "static.hotjar.com": "Hotjar",
    "cdn.mxpnl.com": "Mixpanel",
    "cdn.amplitude.com": "Amplitude",
    "cdn.heapanalytics.com": "Heap",
    "cdn.optimizely.com": "Optimizely",
    "static.zdassets.com": "Zendesk",
    "js.chilipiper.com": "Chili Piper",
    "assets.calendly.com": "Calendly",
    "fast.wistia.com": "Wistia",
    "fast.wistia.net": "Wistia",
    "player.vimeo.com": "Vimeo",
    "youtube.com/embed": "YouTube",
    "youtube-nocookie.com": "YouTube",
    "play.vidyard.com": "Vidyard",
    "js.stripe.com": "Stripe",
    "cdn.shopify.com": "Shopify",
    "assets.website-files.com": "Webflow",
    "assets-global.website-files.com": "Webflow",
    "cdn.prod.website-files.com": "Webflow",
    "/wp-content/": "WordPress",
    "/wp-includes/": "WordPress",
    "/_next/static/": "Next.js",
    "cdn.contentful.com": "Contentful",
    "js.sentry-cdn.com": "Sentry",
    "cdn.cookielaw.org": "OneTrust",
}

# page signal -> patterns matched against link hrefs and link text.
PAGE_SIGNAL_PATTERNS: dict[str, tuple[str, ...]] = {
    "has_pricing_page": (r"/pricing", r"/plans\b", r"\bpricing\b"),
    "has_demo_cta": (r"/demo", r"request[- ]a[- ]demo", r"book[- ]a[- ]demo", r"\bget a demo\b"),
    "has_free_trial": (r"free[- ]trial", r"start[- ]free", r"try[- ]it[- ]free", r"/signup"),
    "has_careers_page": (
        r"/careers", r"/jobs\b", r"\bwe.?re hiring\b",
        r"boards\.greenhouse\.io", r"jobs\.lever\.co",
    ),
    "has_login": (r"/login", r"/signin", r"\bsign in\b", r"\blog in\b"),
    "has_blog": (r"/blog", r"/resources\b", r"/insights\b"),
    "has_case_studies": (r"case[- ]stud", r"/customers\b", r"success[- ]stor"),
    "has_security_page": (r"/security\b", r"\bsoc ?2\b", r"/trust\b", r"\bgdpr\b"),
    "has_integrations": (r"/integrations\b", r"/marketplace\b", r"/apps\b"),
    "has_contact_sales": (r"contact[- ]sales", r"talk[- ]to[- ]sales", r"/contact\b"),
}

_WHITESPACE_RUN = re.compile(r"\n{3,}")
_LINK_SOUP = re.compile(r"^[\s|•·>-]*$")


def content_hash(markdown: str) -> str:
    """Stable hash of page content, so re-analysis can be skipped when nothing changed."""
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:16]


def _pick_main(soup: BeautifulSoup) -> Tag:
    """Prefer <main>, then the longest <article>, else <body>."""
    main = soup.find("main")
    if isinstance(main, Tag) and len(main.get_text(strip=True)) > 200:
        return main
    articles = [a for a in soup.find_all("article") if isinstance(a, Tag)]
    if articles:
        longest = max(articles, key=lambda a: len(a.get_text(strip=True)))
        if len(longest.get_text(strip=True)) > 400:
            return longest
    body = soup.find("body")
    return body if isinstance(body, Tag) else soup


def extract_links(soup: BeautifulSoup, base_url: str) -> list[PageLink]:
    """Absolute links with their anchor text, de-duplicated, junk anchors dropped."""
    seen: set[str] = set()
    out: list[PageLink] = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base_url, href)
        if urlparse(url).scheme not in ("http", "https"):
            continue
        text = " ".join(a.get_text(" ", strip=True).split())[:120]
        if url in seen:
            continue
        seen.add(url)
        out.append(PageLink(text=text, url=url))
    return out


def detect_tech(html: str, base_url: str) -> list[str]:
    """Vendor fingerprints from asset URLs on the page. Deterministic, no LLM."""
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    for tag, attr in (("script", "src"), ("link", "href"), ("img", "src"), ("iframe", "src")):
        for el in soup.find_all(tag):
            value = el.get(attr)
            if value:
                candidates.append(urljoin(base_url, str(value)))
    haystack = "\n".join(candidates).lower()

    found: list[str] = []
    for fragment, label in TECH_SIGNATURES.items():
        if fragment in haystack and label not in found:
            found.append(label)
    return sorted(found)


def detect_page_signals(links: list[PageLink], markdown: str) -> dict[str, bool]:
    """Structural facts about the site, from link targets and anchor text."""
    corpus = "\n".join(f"{link.url} {link.text}" for link in links).lower()
    # A CTA can be a button rendered as text rather than an <a>; check copy too.
    corpus += "\n" + markdown[:4000].lower()
    return {
        signal: any(re.search(p, corpus) for p in patterns)
        for signal, patterns in PAGE_SIGNAL_PATTERNS.items()
    }


def html_to_markdown(
    html: str, base_url: str, max_chars: int = 40_000
) -> tuple[str, dict[str, str | None]]:
    """Render a homepage to markdown and pull out title/description.

    Returns `(markdown, {"title": ..., "meta_description": ...})`.
    """
    soup = BeautifulSoup(html, "html.parser")

    title = None
    if soup.title and soup.title.string:
        title = " ".join(soup.title.string.split())[:300]

    meta_description = None
    for selector in ({"name": "description"}, {"property": "og:description"}):
        tag = soup.find("meta", attrs=selector)
        if isinstance(tag, Tag) and tag.get("content"):
            meta_description = " ".join(str(tag["content"]).split())[:500]
            break

    for tag_name in _NOISE_TAGS:
        for el in soup.find_all(tag_name):
            el.decompose()

    main = _pick_main(soup)
    md = markdownify(str(main), heading_style="ATX", strip=["img"])

    # markdownify leaves long runs of blank lines and nav separator debris.
    lines = [ln.rstrip() for ln in md.splitlines()]
    lines = [ln for ln in lines if not _LINK_SOUP.match(ln) or ln == ""]
    md = _WHITESPACE_RUN.sub("\n\n", "\n".join(lines)).strip()

    if len(md) > max_chars:
        md = md[:max_chars].rsplit("\n", 1)[0] + "\n\n_[truncated]_"

    return md, {"title": title, "meta_description": meta_description}
