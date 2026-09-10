"""Path probing: does this page exist, and can we trust the answer?

The interesting cases are all the ones where a `200` is a lie. Each test names
a real site behaviour observed while building this.
"""

from __future__ import annotations

import pytest

from gtm_enrich.scrape.fetchers import FetchedContent, Fetcher, FetcherError
from gtm_enrich.scrape.probe import (
    MIN_CONTROL_TEXT,
    Control,
    ControlMode,
    PathProber,
    Verdict,
    similarity,
    tokenize,
)

# Says so in words. The easy case -- the marker check catches it.
NOT_FOUND_PAGE = (
    "Acme\n\nWe couldn't find that page. Try the homepage, or browse our "
    "product, pricing, and customers sections. Contact support if you think "
    "this is a mistake. Popular links: docs, changelog, status."
)

# The hard case, and the one this module exists for: a branded catch-all that
# renders the site's normal chrome, returns 200, and never admits anything is
# wrong. No marker will catch this. Only comparing it to the control page will.
SOFT_404_PAGE = (
    "Acme\n\nProduct Platform Solutions Customers Docs Company\n\n"
    "Build faster with Acme. The platform teams trust to ship software. "
    "Join thousands of teams already building with Acme.\n\n"
    "Get started Contact sales\n\n"
    "Company About Careers Blog Press Legal Privacy Terms Security Status"
)

REAL_PRICING = (
    "Acme Pricing\n\nSimple plans that scale with your team.\n\n"
    "Starter $19 per month per user, billed annually. Includes unlimited "
    "projects, integrations, and email support.\n\n"
    "Growth $49 per month per user. Adds SSO, audit logs, advanced analytics, "
    "and a dedicated success manager.\n\n"
    "Enterprise: contact sales for custom billing, procurement, and SLAs. "
    "Every plan starts with a 14 day free trial, no card required. "
    "Compare all plans and features below."
)


class FakeFetcher(Fetcher):
    """Serves canned responses keyed by URL path, like a tiny fake website."""

    name = "fake"

    def __init__(self, pages: dict[str, FetchedContent], default=None) -> None:
        self.pages = pages
        self.default = default
        self.requested: list[str] = []

    async def fetch(self, url: str) -> FetchedContent:
        self.requested.append(url)
        path = "/" + url.split("/", 3)[3] if url.count("/") > 2 else "/"
        if path in self.pages:
            return self.pages[path]
        if self.default is None:
            raise FetcherError(f"{url}: HTTP 404")
        return self.default


def page(markdown: str, status: int = 200, final_url: str = "https://acme.example/x"):
    return FetchedContent(final_url=final_url, status_code=status, markdown=markdown)


# --------------------------------------------------------------------------- #
# similarity
# --------------------------------------------------------------------------- #


def test_identical_text_is_perfectly_similar() -> None:
    assert similarity(tokenize(NOT_FOUND_PAGE), tokenize(NOT_FOUND_PAGE)) == 1.0


def test_unrelated_text_is_dissimilar() -> None:
    assert similarity(tokenize(NOT_FOUND_PAGE), tokenize(REAL_PRICING)) < 0.3


def test_similarity_of_empty_input_is_zero() -> None:
    assert similarity(set(), tokenize(REAL_PRICING)) == 0.0


# --------------------------------------------------------------------------- #
# calibration
# --------------------------------------------------------------------------- #


async def test_a_site_that_404s_honestly_is_detected() -> None:
    """stripe.com, gong.io: the status code means what it says."""
    prober = PathProber(FakeFetcher({}))  # every unknown path raises
    control = await prober.calibrate("acme.example")
    assert control.mode is ControlMode.HONEST
    assert control.similarity_usable is False


async def test_a_site_that_200s_everything_is_detected() -> None:
    """linear.app, vercel.com: 200 for a random 32-char path."""
    prober = PathProber(FakeFetcher({}, default=page(SOFT_404_PAGE)))
    control = await prober.calibrate("acme.example")
    assert control.mode is ControlMode.SOFT
    assert control.similarity_usable is True
    assert control.tokens


async def test_a_control_page_that_renders_nothing_is_flagged_blind() -> None:
    """notion.so over plain HTTP: 200, and an empty JS shell."""
    prober = PathProber(FakeFetcher({}, default=page("Acme")))
    control = await prober.calibrate("acme.example")
    assert control.mode is ControlMode.BLIND
    assert control.text_length < MIN_CONTROL_TEXT
    assert control.similarity_usable is False


async def test_calibration_asks_for_a_path_that_cannot_exist() -> None:
    fetcher = FakeFetcher({}, default=page(NOT_FOUND_PAGE))
    await PathProber(fetcher).calibrate("acme.example")
    requested = fetcher.requested[0]
    assert requested.startswith("https://acme.example/")
    nonce = requested.rsplit("/", 1)[1]
    assert len(nonce) == 32 and all(c in "0123456789abcdef" for c in nonce)


async def test_two_calibrations_use_different_nonces() -> None:
    """A fixed path could be cached, or could genuinely exist on some site."""
    fetcher = FakeFetcher({}, default=page(NOT_FOUND_PAGE))
    prober = PathProber(fetcher)
    await prober.calibrate("acme.example")
    await prober.calibrate("acme.example")
    assert fetcher.requested[0] != fetcher.requested[1]


# --------------------------------------------------------------------------- #
# verdicts
# --------------------------------------------------------------------------- #


async def test_soft_404_is_caught_by_similarity_to_the_control() -> None:
    """The whole point: 200, plenty of text, no not-found copy anywhere, and
    still not a real page. Nothing but the control comparison catches this."""
    fetcher = FakeFetcher({"/pricing": page(SOFT_404_PAGE)}, default=page(SOFT_404_PAGE))
    prober = PathProber(fetcher)
    control = await prober.calibrate("acme.example")
    result = await prober.probe("acme.example", "/pricing", control)

    assert result.verdict is Verdict.MISSING
    assert result.similarity_to_control > 0.85
    assert "soft 404" in result.reason
    assert result.confidence >= 0.9


async def test_a_real_page_survives_the_same_check() -> None:
    fetcher = FakeFetcher({"/pricing": page(REAL_PRICING)}, default=page(SOFT_404_PAGE))
    prober = PathProber(fetcher)
    control = await prober.calibrate("acme.example")
    result = await prober.probe(
        "acme.example", "/pricing", control, topic_terms=("per month", "billing")
    )

    assert result.verdict is Verdict.EXISTS
    assert result.similarity_to_control < 0.5
    assert result.confidence >= 0.9


async def test_honest_404_is_missing() -> None:
    prober = PathProber(FakeFetcher({}))
    control = await prober.calibrate("acme.example")
    result = await prober.probe("acme.example", "/pricing", control)
    assert result.verdict is Verdict.MISSING


async def test_a_login_wall_is_gated_not_missing() -> None:
    """notion.so/packages. 'We can't tell' is not the same as 'it isn't there'."""
    wall = page(
        "Acme\n\nYou're almost there! Sign in to see this page in your workspace. "
        "Continue with Google, Apple, or SSO. New user? Sign up free today."
    )
    fetcher = FakeFetcher({"/pricing": wall}, default=page(SOFT_404_PAGE))
    prober = PathProber(fetcher)
    result = await prober.probe(
        "acme.example", "/pricing", await prober.calibrate("acme.example")
    )
    assert result.verdict is Verdict.GATED
    assert result.exists is False
    assert "login wall" in result.reason


async def test_redirect_to_the_homepage_is_missing() -> None:
    landed_home = FetchedContent(
        final_url="https://acme.example/", status_code=200, markdown=REAL_PRICING
    )
    fetcher = FakeFetcher({"/pricing": landed_home}, default=page(SOFT_404_PAGE))
    prober = PathProber(fetcher)
    result = await prober.probe(
        "acme.example", "/pricing", await prober.calibrate("acme.example")
    )
    assert result.verdict is Verdict.MISSING
    assert "homepage" in result.reason


async def test_not_found_copy_is_caught_even_when_similarity_is_unavailable() -> None:
    """The marker check has to work on blind-control sites too."""
    fetcher = FakeFetcher({"/pricing": page(NOT_FOUND_PAGE)}, default=page("Acme"))
    prober = PathProber(fetcher)
    control = await prober.calibrate("acme.example")
    assert control.mode is ControlMode.BLIND

    result = await prober.probe("acme.example", "/pricing", control)
    assert result.verdict is Verdict.MISSING
    assert "not-found copy" in result.reason


async def test_a_stub_page_is_missing() -> None:
    fetcher = FakeFetcher({"/pricing": page("Pricing")}, default=page(SOFT_404_PAGE))
    prober = PathProber(fetcher)
    result = await prober.probe(
        "acme.example", "/pricing", await prober.calibrate("acme.example")
    )
    assert result.verdict is Verdict.MISSING
    assert "chars of content" in result.reason


async def test_blind_control_lowers_confidence_on_a_positive() -> None:
    """Without a usable control we cannot rule out a soft 404, and we say so."""
    fetcher = FakeFetcher({"/pricing": page(REAL_PRICING)}, default=page("Acme"))
    prober = PathProber(fetcher)
    control = await prober.calibrate("acme.example")

    blind = await prober.probe("acme.example", "/pricing", control)
    assert blind.verdict is Verdict.EXISTS
    assert blind.confidence == 0.5
    assert "cannot be ruled out" in blind.reason


async def test_off_topic_content_lowers_confidence() -> None:
    fetcher = FakeFetcher(
        {"/pricing": page("Acme blog: ten ways to run a better standup meeting. " * 6)},
        default=page(SOFT_404_PAGE),
    )
    prober = PathProber(fetcher)
    result = await prober.probe(
        "acme.example", "/pricing", await prober.calibrate("acme.example"),
        topic_terms=("per month", "billing"),
    )
    assert result.verdict is Verdict.EXISTS
    assert result.confidence == 0.6
    assert "off-topic" in result.reason


# --------------------------------------------------------------------------- #
# batching
# --------------------------------------------------------------------------- #


async def test_probe_many_calibrates_once_for_the_whole_batch() -> None:
    """The control request is the cost of the guard; pay it once per domain."""
    fetcher = FakeFetcher(
        {"/pricing": page(REAL_PRICING), "/careers": page(SOFT_404_PAGE)},
        default=page(SOFT_404_PAGE),
    )
    prober = PathProber(fetcher)
    results = await prober.probe_many(
        "acme.example", {"/pricing": ("per month",), "/careers": ("hiring",)}
    )

    assert results["/pricing"].verdict is Verdict.EXISTS
    assert results["/careers"].verdict is Verdict.MISSING
    assert len(fetcher.requested) == 3  # one control + two paths


async def test_probe_many_accepts_a_precomputed_control() -> None:
    fetcher = FakeFetcher({"/pricing": page(REAL_PRICING)}, default=page(SOFT_404_PAGE))
    prober = PathProber(fetcher)
    control = Control(mode=ControlMode.HONEST, status_code=404)
    await prober.probe_many("acme.example", {"/pricing": ()}, control)
    assert len(fetcher.requested) == 1  # no calibration request


@pytest.mark.parametrize("verdict", list(Verdict))
def test_only_exists_counts_as_exists(verdict: Verdict) -> None:
    from gtm_enrich.scrape.probe import PathProbe

    probe = PathProbe("/p", "https://x/p", verdict, "", 1.0)
    assert probe.exists is (verdict is Verdict.EXISTS)
