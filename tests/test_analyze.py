"""Analysis: the keyword fallback, and the model call with a stubbed SDK client."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import pytest

from gtm_enrich.analyze.heuristics import analyze_with_heuristics
from gtm_enrich.analyze.llm import AnalysisError, analyze_with_llm
from gtm_enrich.analyze.prompt import build_system, build_user
from gtm_enrich.config import AnalyzeSettings, IcpProfile
from gtm_enrich.models import HomepageAnalysis

# --------------------------------------------------------------------------- #
# heuristics
# --------------------------------------------------------------------------- #


def test_heuristics_produce_a_valid_analysis(sample_page, page_signals, icp) -> None:
    analysis, provenance = analyze_with_heuristics(sample_page, page_signals, icp)
    assert isinstance(analysis, HomepageAnalysis)
    assert analysis.company_name == "Acme Analytics"
    assert analysis.sells_to == "B2B"
    assert analysis.business_model == "SaaS"  # has both a pricing page and a login
    assert analysis.primary_cta == "Book a demo"
    assert provenance.analyzer == "heuristic:v1"


def test_heuristics_never_claim_high_confidence(sample_page, page_signals, icp) -> None:
    analysis, _ = analyze_with_heuristics(sample_page, page_signals, icp)
    assert analysis.confidence <= 0.4
    assert "no model was used" in analysis.icp_fit_rationale


def test_heuristics_flag_hiring_from_a_greenhouse_link(sample_page, page_signals, icp) -> None:
    analysis, _ = analyze_with_heuristics(sample_page, page_signals, icp)
    assert any("hiring" in s.lower() for s in analysis.buying_signals)


def test_heuristics_penalize_poor_fit_terms(sample_page, page_signals) -> None:
    neutral = IcpProfile(
        name="t", description="d", good_fit=[], poor_fit=[],
        heuristic_good=["webinar"], heuristic_poor=[],
    )
    competitor = IcpProfile(
        name="t", description="d", good_fit=[], poor_fit=[],
        heuristic_good=["webinar"], heuristic_poor=["product analytics"],
    )
    base, _ = analyze_with_heuristics(sample_page, page_signals, neutral)
    docked, _ = analyze_with_heuristics(sample_page, page_signals, competitor)
    assert docked.icp_fit_score < base.icp_fit_score
    assert docked.disqualifiers


def test_heuristics_handle_a_thin_page(sample_page, icp) -> None:
    sample_page.markdown = "# Coming soon"
    analysis, _ = analyze_with_heuristics(sample_page, {}, icp)
    assert analysis.confidence == 0.2
    assert any("thin" in d.lower() for d in analysis.disqualifiers)


# --------------------------------------------------------------------------- #
# prompt
# --------------------------------------------------------------------------- #


def test_system_prompt_carries_the_icp(icp) -> None:
    system = build_system(icp)
    assert icp.name in system
    assert "Never invent a buying signal" in system
    # Heuristic-only keywords must not leak into the prompt.
    assert "heuristic_keywords" not in system


def test_user_prompt_includes_page_and_structural_signals(sample_page, page_signals) -> None:
    user = build_user(sample_page, page_signals)
    assert sample_page.markdown[:80] in user
    assert "has_pricing_page: true" in user
    assert "HubSpot" in user  # detected vendors are handed over as facts


# --------------------------------------------------------------------------- #
# LLM transport
# --------------------------------------------------------------------------- #


def fake_response(analysis: HomepageAnalysis, **overrides):
    payload = {
        "parsed_output": analysis,
        "stop_reason": "end_turn",
        "stop_details": None,
        "model": "claude-opus-5",
        "usage": SimpleNamespace(
            input_tokens=5_000, output_tokens=600, cache_read_input_tokens=0
        ),
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


class FakeClient:
    """Stands in for `anthropic.Anthropic`, recording the request it was given."""

    def __init__(self, response=None, raises=None) -> None:
        self.calls: list[dict] = []
        self._response = response
        self._raises = raises
        parse = self._parse
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None and len(self.calls) == 1:
            raise self._raises
        return self._response


def bad_request(message: str) -> anthropic.BadRequestError:
    return anthropic.BadRequestError(
        message,
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com")),
        body=None,
    )


def test_llm_request_shape(sample_page, page_signals, icp, analysis) -> None:
    client = FakeClient(response=fake_response(analysis))
    settings = AnalyzeSettings(model="claude-opus-5", effort="medium")

    result, provenance = analyze_with_llm(
        sample_page, page_signals, build_system(icp), settings, client=client
    )

    call = client.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["output_format"] is HomepageAnalysis
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "medium"}
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    # The ICP block is the stable prefix, so it carries the cache breakpoint.
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert result is analysis
    assert provenance.analyzer == "llm:claude-opus-5"


def test_llm_records_tokens_and_cost(sample_page, page_signals, icp, analysis) -> None:
    client = FakeClient(response=fake_response(analysis))
    _, provenance = analyze_with_llm(
        sample_page, page_signals, build_system(icp), AnalyzeSettings(), client=client
    )
    assert provenance.input_tokens == 5_000
    assert provenance.output_tokens == 600
    # 5000 in @ $5/MTok + 600 out @ $25/MTok
    assert provenance.cost_usd == pytest.approx(0.04)


def test_llm_counts_cache_reads_toward_input_tokens(sample_page, page_signals, icp, analysis):
    usage = SimpleNamespace(input_tokens=200, output_tokens=100, cache_read_input_tokens=4_800)
    client = FakeClient(response=fake_response(analysis, usage=usage))
    _, provenance = analyze_with_llm(
        sample_page, page_signals, build_system(icp), AnalyzeSettings(), client=client
    )
    assert provenance.input_tokens == 5_000


def test_llm_raises_on_refusal(sample_page, page_signals, icp, analysis) -> None:
    client = FakeClient(
        response=fake_response(
            analysis, stop_reason="refusal", stop_details=SimpleNamespace(category="cyber")
        )
    )
    with pytest.raises(AnalysisError, match="declined"):
        analyze_with_llm(
            sample_page, page_signals, build_system(icp), AnalyzeSettings(), client=client
        )


def test_llm_retries_without_the_fallback_beta_if_rejected(
    sample_page, page_signals, icp, analysis
) -> None:
    client = FakeClient(
        response=fake_response(analysis),
        raises=bad_request("fallbacks is not supported for this account"),
    )
    result, _ = analyze_with_llm(
        sample_page, page_signals, build_system(icp), AnalyzeSettings(), client=client
    )
    assert result is analysis
    assert len(client.calls) == 2
    assert "fallbacks" in client.calls[0]
    assert "fallbacks" not in client.calls[1]  # retried clean
    assert "betas" not in client.calls[1]


def test_llm_does_not_retry_unrelated_bad_requests(
    sample_page, page_signals, icp, analysis
) -> None:
    client = FakeClient(
        response=fake_response(analysis), raises=bad_request("max_tokens is too large")
    )
    with pytest.raises(AnalysisError, match="rejected the request"):
        analyze_with_llm(
            sample_page, page_signals, build_system(icp), AnalyzeSettings(), client=client
        )
    assert len(client.calls) == 1


def test_llm_raises_when_no_parsed_output(sample_page, page_signals, icp, analysis) -> None:
    client = FakeClient(
        response=fake_response(analysis, parsed_output=None, stop_reason="max_tokens")
    )
    with pytest.raises(AnalysisError, match="No parsed output"):
        analyze_with_llm(
            sample_page, page_signals, build_system(icp), AnalyzeSettings(), client=client
        )
