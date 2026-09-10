"""Does this page actually exist?

A surprising number of sites answer `200 OK` for a URL that does not exist. Some
are SPAs with a catch-all route, some render a branded "we couldn't find that"
page without changing the status. Measured across seven well-known B2B sites,
three of them returned `200` for a random 32-character path. On those, a status
code carries no information at all.

The guard is a **control probe**. Before checking anything real, ask the site for
a URL that certainly does not exist -- a random hex path -- and keep what comes
back. That one extra request calibrates every later check against how *this*
site behaves, rather than against an assumption about how sites behave:

* control returns 4xx -> the site has honest 404s, trust the status code
* control returns 2xx with content -> the site soft-404s, so compare content
* control returns 2xx and renders nothing -> similarity is useless here, fall
  back to markers and length, and say the confidence is low

Measured separation on real sites, comparing a candidate page's text against the
control page's text (Jaccard over word tokens):

    linear.app   /pricing  0.000    /packages  1.000
    vercel.com   /pricing  0.020    /packages  0.926

Real pages sit near zero, soft 404s near one. A threshold of 0.85 has enormous
headroom in both directions.

The third verdict matters as much as the first two: `notion.so/packages` is
neither a real page nor a 404, it is a login wall. "We cannot tell" is a
different answer from "it isn't there", and collapsing them produces exactly the
kind of confident-and-wrong CRM field this whole project exists to avoid.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse

from .fetchers import Fetcher, FetcherError

log = logging.getLogger(__name__)

# Similarity above this to the control page means we are looking at the site's
# not-found page wearing a different URL.
SOFT_404_SIMILARITY = 0.85
# A real page has more to say than this. Matches the threshold used by the
# in-platform version of this check.
MIN_PAGE_TEXT = 200
# Below this, the control page rendered nothing useful and similarity is noise.
MIN_CONTROL_TEXT = 60

_TOKEN_RE = re.compile(r"[a-z0-9']{3,}")

# Checked against the opening of the page, where a not-found message lives.
_MISSING_MARKERS = (
    "page not found", "not found", "404", "page doesn't exist", "page does not exist",
    "no longer available", "couldn't find", "could not find", "page you requested",
    "nothing here", "went wrong",
)
_GATED_MARKERS = (
    "sign in to", "log in to", "sign in with", "you're almost there",
    "create an account", "request access", "members only", "subscribe to continue",
)
_MARKER_WINDOW = 400


class Verdict(str, Enum):
    EXISTS = "exists"
    MISSING = "missing"
    GATED = "gated"
    UNKNOWN = "unknown"


class ControlMode(str, Enum):
    HONEST = "honest_404"        # status codes mean something here
    SOFT = "soft_404"            # 200 for everything; compare content
    BLIND = "soft_404_blind"     # 200 for everything and renders nothing


@dataclass
class Control:
    """What this site does when asked for a URL that cannot exist."""

    mode: ControlMode
    status_code: int | None
    tokens: set[str] = field(default_factory=set)
    text_length: int = 0

    @property
    def similarity_usable(self) -> bool:
        return self.mode is ControlMode.SOFT


@dataclass
class PathProbe:
    """One answer about one path, with the reasoning attached."""

    path: str
    url: str
    verdict: Verdict
    reason: str
    confidence: float
    status_code: int | None = None
    text_length: int = 0
    similarity_to_control: float | None = None

    @property
    def exists(self) -> bool:
        return self.verdict is Verdict.EXISTS


def tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def similarity(a: set[str], b: set[str]) -> float:
    """Jaccard overlap. Two renderings of the same not-found page score ~1.0."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _first_marker(text: str, markers: tuple[str, ...]) -> str | None:
    window = (text or "")[:_MARKER_WINDOW].lower()
    return next((m for m in markers if m in window), None)


def _is_root(url: str) -> bool:
    return urlparse(url).path.strip("/") == ""


class PathProber:
    """Checks whether specific paths exist on a site, guarding against soft 404s.

    Calibrate once per domain, then probe as many paths as you like. The
    calibration request is the cost of the guard: one extra fetch per domain,
    amortized across every path you check.
    """

    def __init__(self, fetcher: Fetcher, *, concurrency: int = 4) -> None:
        self._fetcher = fetcher
        self._semaphore = asyncio.Semaphore(concurrency)

    async def calibrate(self, domain: str) -> Control:
        """Ask for a URL that cannot exist, and see what the site says."""
        nonce = secrets.token_hex(16)
        url = f"https://{domain}/{nonce}"
        try:
            content = await self._fetcher.fetch(url)
        except FetcherError as exc:
            # A backend that refuses the control page (404 raised as an error)
            # is itself the signal: this site 404s honestly.
            log.debug("control probe for %s failed, assuming honest 404s: %s", domain, exc)
            return Control(mode=ControlMode.HONEST, status_code=None)

        text = content.markdown or content.html or ""
        if content.status_code >= 400:
            return Control(ControlMode.HONEST, content.status_code, text_length=len(text))
        if len(text) < MIN_CONTROL_TEXT:
            return Control(ControlMode.BLIND, content.status_code, text_length=len(text))
        return Control(
            ControlMode.SOFT, content.status_code, tokens=tokenize(text), text_length=len(text)
        )

    async def probe(
        self,
        domain: str,
        path: str,
        control: Control,
        topic_terms: tuple[str, ...] = (),
    ) -> PathProbe:
        """Decide whether `path` is a real page on `domain`."""
        url = f"https://{domain}{path}"
        async with self._semaphore:
            try:
                content = await self._fetcher.fetch(url)
            except FetcherError as exc:
                return PathProbe(path, url, Verdict.MISSING, f"fetch failed: {exc}", 0.9)

        text = content.markdown or content.html or ""
        sim = (
            similarity(tokenize(text), control.tokens) if control.similarity_usable else None
        )
        probe = PathProbe(
            path=path,
            url=url,
            verdict=Verdict.UNKNOWN,
            reason="",
            confidence=0.5,
            status_code=content.status_code,
            text_length=len(text),
            similarity_to_control=sim,
        )

        # Ordered from most to least decisive. The first one that fires wins,
        # and its reason is what gets recorded.
        if content.status_code >= 400:
            return self._set(probe, Verdict.MISSING, f"HTTP {content.status_code}", 0.98)

        if _is_root(content.final_url) and path.strip("/"):
            return self._set(probe, Verdict.MISSING, "redirected to the homepage", 0.95)

        gated = _first_marker(text, _GATED_MARKERS)
        if gated:
            # Not absent -- unreadable. Those are different answers.
            return self._set(probe, Verdict.GATED, f"login wall ({gated!r})", 0.85)

        missing = _first_marker(text, _MISSING_MARKERS)
        if missing:
            return self._set(probe, Verdict.MISSING, f"not-found copy ({missing!r})", 0.9)

        if sim is not None and sim > SOFT_404_SIMILARITY:
            return self._set(
                probe, Verdict.MISSING, f"soft 404: {sim:.2f} similar to the control page", 0.95
            )

        if len(text) < MIN_PAGE_TEXT:
            return self._set(
                probe, Verdict.MISSING, f"only {len(text)} chars of content", 0.8
            )

        # It looks like a real page. How sure we are depends on whether the
        # content is on-topic and whether similarity was available to rule out
        # a soft 404 in the first place.
        on_topic = any(t in text.lower() for t in topic_terms) if topic_terms else None
        if on_topic:
            return self._set(probe, Verdict.EXISTS, "on-topic content found", 0.95)
        if control.mode is ControlMode.BLIND:
            return self._set(
                probe,
                Verdict.EXISTS,
                "looks real, but the control page rendered nothing so soft 404s "
                "cannot be ruled out",
                0.5,
            )
        if on_topic is False:
            return self._set(probe, Verdict.EXISTS, "real page, but off-topic content", 0.6)
        return self._set(probe, Verdict.EXISTS, "real page", 0.8)

    @staticmethod
    def _set(probe: PathProbe, verdict: Verdict, reason: str, confidence: float) -> PathProbe:
        probe.verdict = verdict
        probe.reason = reason
        probe.confidence = confidence
        return probe

    async def probe_many(
        self,
        domain: str,
        paths: dict[str, tuple[str, ...]],
        control: Control | None = None,
    ) -> dict[str, PathProbe]:
        """Probe several paths, calibrating first if a control was not supplied.

        `paths` maps a path to the vocabulary that would confirm it is on topic,
        e.g. `{"/pricing": ("per month", "plan", "billing")}`.
        """
        control = control or await self.calibrate(domain)
        results = await asyncio.gather(
            *(self.probe(domain, p, control, terms) for p, terms in paths.items())
        )
        return {r.path: r for r in results}


# Paths worth checking, with the vocabulary that confirms each one is on topic.
COMMON_PATHS: dict[str, tuple[str, ...]] = {
    "/pricing": ("per month", "per user", "plan", "billing", "free trial", "/mo"),
    "/plans": ("per month", "plan", "billing", "subscription"),
    "/careers": ("open role", "join us", "hiring", "apply", "position"),
    "/customers": ("case study", "customer story", "results", "testimonial"),
    "/security": ("soc 2", "encryption", "compliance", "gdpr", "iso 27001"),
    "/integrations": ("connect", "integration", "api", "sync"),
}
