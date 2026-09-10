"""Analysis: the keyword fallback, and the model call with a stubbed SDK client."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest

from gtm_enrich.analyze.heuristics import analyze_with_heuristics
from gtm_enrich.analyze.llm import analyze_with_llm
from gtm_enrich.analyze.prompt import build_system, build_user
from gtm_enrich.analyze.providers import (
    PROVIDER_NAMES,
    AnalysisError,
    AnthropicProvider,
    OpenAIProvider,
    ProviderNotConfigured,
    build_provider,
    default_model_for,
)
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
# providers — shared behaviour
# --------------------------------------------------------------------------- #


def fake_anthropic_response(analysis: HomepageAnalysis, **overrides):
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


class FakeAnthropic:
    """Stands in for `anthropic.Anthropic`, recording the request it was given."""

    def __init__(self, response=None, raises=None) -> None:
        self.calls: list[dict] = []
        self._response = response
        self._raises = raises
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None and len(self.calls) == 1:
            raise self._raises
        return self._response


def fake_openai_response(analysis: HomepageAnalysis | None, **overrides):
    payload = {
        "output_parsed": analysis,
        "status": "completed",
        "incomplete_details": None,
        "model": "gpt-5-mini",
        "usage": SimpleNamespace(
            input_tokens=5_000,
            output_tokens=600,
            input_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


class FakeOpenAI:
    """Stands in for `openai.OpenAI`."""

    def __init__(self, response=None, raises=None) -> None:
        self.calls: list[dict] = []
        self._response = response
        self._raises = raises
        self.responses = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._response


def anthropic_bad_request(message: str) -> anthropic.BadRequestError:
    return anthropic.BadRequestError(
        message,
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com")),
        body=None,
    )


def openai_bad_request(message: str) -> openai.BadRequestError:
    return openai.BadRequestError(
        message,
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.openai.com")),
        body=None,
    )


@pytest.mark.parametrize("name", ["anthropic", "openai"])
def test_every_provider_is_registered_with_a_default_model(name: str) -> None:
    assert name in PROVIDER_NAMES
    assert default_model_for(name)


def test_unknown_provider_is_a_clean_error() -> None:
    with pytest.raises(ProviderNotConfigured, match="Unknown LLM provider"):
        build_provider("pplx")


@pytest.mark.parametrize(
    ("provider_factory", "fake"),
    [
        (lambda c: AnthropicProvider(client=c), "anthropic"),
        (lambda c: OpenAIProvider(client=c), "openai"),
    ],
)
def test_providers_return_the_same_shape(provider_factory, fake, analysis) -> None:
    """Whatever the vendor, the pipeline sees one normalized result."""
    client = (
        FakeAnthropic(response=fake_anthropic_response(analysis))
        if fake == "anthropic"
        else FakeOpenAI(response=fake_openai_response(analysis))
    )
    provider = provider_factory(client)
    result = provider.analyze("system", "user", provider.default_model, "medium", 8_000)

    assert result.analysis is analysis
    assert result.billed_input_tokens == 5_000
    assert result.output_tokens == 600


# --------------------------------------------------------------------------- #
# providers — Anthropic specifics
# --------------------------------------------------------------------------- #


def test_anthropic_request_shape(analysis) -> None:
    client = FakeAnthropic(response=fake_anthropic_response(analysis))
    AnthropicProvider(client=client).analyze("SYS", "USER", "claude-opus-5", "medium", 8_000)

    call = client.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["output_format"] is HomepageAnalysis
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "medium"}
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    # The ICP block is the stable prefix, so it carries the cache breakpoint.
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_counts_cache_reads_toward_input_tokens(analysis) -> None:
    usage = SimpleNamespace(input_tokens=200, output_tokens=100, cache_read_input_tokens=4_800)
    client = FakeAnthropic(response=fake_anthropic_response(analysis, usage=usage))
    result = AnthropicProvider(client=client).analyze("s", "u", "claude-opus-5", "medium", 8_000)
    assert result.billed_input_tokens == 5_000
    assert result.cached_input_tokens == 4_800


def test_anthropic_raises_on_refusal(analysis) -> None:
    client = FakeAnthropic(
        response=fake_anthropic_response(
            analysis, stop_reason="refusal", stop_details=SimpleNamespace(category="cyber")
        )
    )
    with pytest.raises(AnalysisError, match="declined"):
        AnthropicProvider(client=client).analyze("s", "u", "claude-opus-5", "medium", 8_000)


def test_anthropic_retries_without_the_fallback_beta_if_rejected(analysis) -> None:
    client = FakeAnthropic(
        response=fake_anthropic_response(analysis),
        raises=anthropic_bad_request("fallbacks is not supported for this account"),
    )
    result = AnthropicProvider(client=client).analyze("s", "u", "claude-opus-5", "medium", 8_000)

    assert result.analysis is analysis
    assert len(client.calls) == 2
    assert "fallbacks" in client.calls[0]
    assert "fallbacks" not in client.calls[1]  # retried clean
    assert "betas" not in client.calls[1]


def test_anthropic_does_not_retry_unrelated_bad_requests(analysis) -> None:
    client = FakeAnthropic(
        response=fake_anthropic_response(analysis),
        raises=anthropic_bad_request("max_tokens is too large"),
    )
    with pytest.raises(AnalysisError, match="rejected the request"):
        AnthropicProvider(client=client).analyze("s", "u", "claude-opus-5", "medium", 8_000)
    assert len(client.calls) == 1


def test_anthropic_raises_when_no_parsed_output(analysis) -> None:
    client = FakeAnthropic(
        response=fake_anthropic_response(analysis, parsed_output=None, stop_reason="max_tokens")
    )
    with pytest.raises(AnalysisError, match="No parsed output"):
        AnthropicProvider(client=client).analyze("s", "u", "claude-opus-5", "medium", 8_000)


# --------------------------------------------------------------------------- #
# providers — OpenAI specifics
# --------------------------------------------------------------------------- #


def test_openai_request_shape(analysis) -> None:
    client = FakeOpenAI(response=fake_openai_response(analysis))
    OpenAIProvider(client=client).analyze("SYS", "USER", "gpt-5-mini", "medium", 8_000)

    call = client.calls[0]
    assert call["model"] == "gpt-5-mini"
    assert call["text_format"] is HomepageAnalysis
    assert call["max_output_tokens"] == 8_000
    assert call["reasoning"] == {"effort": "medium"}
    # The Responses API has no system field; role "system" goes first in input.
    assert call["input"][0] == {"role": "system", "content": "SYS"}
    assert call["input"][1]["role"] == "user"


@pytest.mark.parametrize(
    ("given", "expected"),
    [("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "high"), ("max", "high")],
)
def test_openai_maps_anthropic_effort_levels(given, expected, analysis) -> None:
    """--effort xhigh is valid for Claude and not for GPT; it must not 400."""
    client = FakeOpenAI(response=fake_openai_response(analysis))
    OpenAIProvider(client=client).analyze("s", "u", "gpt-5-mini", given, 8_000)
    assert client.calls[0]["reasoning"] == {"effort": expected}


def test_openai_normalizes_cached_tokens_to_the_anthropic_split(analysis) -> None:
    """OpenAI reports cached tokens inside input_tokens; Anthropic reports them beside it."""
    usage = SimpleNamespace(
        input_tokens=5_000,
        output_tokens=600,
        input_tokens_details=SimpleNamespace(cached_tokens=4_800),
    )
    client = FakeOpenAI(response=fake_openai_response(analysis, usage=usage))
    result = OpenAIProvider(client=client).analyze("s", "u", "gpt-5-mini", "medium", 8_000)

    assert result.cached_input_tokens == 4_800
    assert result.input_tokens == 200
    assert result.billed_input_tokens == 5_000  # not 9_800


def test_openai_raises_on_incomplete_response(analysis) -> None:
    client = FakeOpenAI(
        response=fake_openai_response(
            None, status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        )
    )
    with pytest.raises(AnalysisError, match="incomplete response \\(max_output_tokens\\)"):
        OpenAIProvider(client=client).analyze("s", "u", "gpt-5-mini", "medium", 8_000)


def test_openai_raises_when_no_parsed_output() -> None:
    client = FakeOpenAI(response=fake_openai_response(None))
    with pytest.raises(AnalysisError, match="No parsed output"):
        OpenAIProvider(client=client).analyze("s", "u", "gpt-5-mini", "medium", 8_000)


def test_openai_api_error_becomes_analysis_error(analysis) -> None:
    client = FakeOpenAI(raises=openai_bad_request("unknown model"))
    with pytest.raises(AnalysisError, match="rejected the request"):
        OpenAIProvider(client=client).analyze("s", "u", "gpt-5-mini", "medium", 8_000)


# --------------------------------------------------------------------------- #
# dispatch + provenance
# --------------------------------------------------------------------------- #


def test_analyze_with_llm_records_provider_in_provenance(
    sample_page, page_signals, icp, analysis
) -> None:
    provider = AnthropicProvider(client=FakeAnthropic(response=fake_anthropic_response(analysis)))
    settings = AnalyzeSettings(provider="anthropic", model="claude-opus-5")

    result, provenance = analyze_with_llm(
        sample_page, page_signals, build_system(icp), settings, provider=provider
    )
    assert result is analysis
    assert provenance.analyzer == "anthropic:claude-opus-5"
    assert provenance.input_tokens == 5_000
    # 5000 in @ $5/MTok + 600 out @ $25/MTok
    assert provenance.cost_usd == pytest.approx(0.04)


def test_analyze_with_llm_prices_openai_models_too(
    sample_page, page_signals, icp, analysis
) -> None:
    provider = OpenAIProvider(client=FakeOpenAI(response=fake_openai_response(analysis)))
    settings = AnalyzeSettings(provider="openai", model="gpt-5-mini")

    _, provenance = analyze_with_llm(
        sample_page, page_signals, build_system(icp), settings, provider=provider
    )
    assert provenance.analyzer == "openai:gpt-5-mini"
    # 5000 in @ $0.25/MTok + 600 out @ $2/MTok
    assert provenance.cost_usd == pytest.approx(0.00245)


def test_unknown_model_yields_no_cost_estimate(sample_page, page_signals, icp, analysis) -> None:
    response = fake_openai_response(analysis, model="gpt-6-experimental")
    provider = OpenAIProvider(client=FakeOpenAI(response=response))
    settings = AnalyzeSettings(provider="openai", model="gpt-6-experimental")

    _, provenance = analyze_with_llm(
        sample_page, page_signals, build_system(icp), settings, provider=provider
    )
    # Better to report nothing than to invent a price.
    assert provenance.cost_usd is None
    assert provenance.input_tokens == 5_000


def test_settings_model_none_falls_back_to_provider_default(
    sample_page, page_signals, icp, analysis
) -> None:
    client = FakeOpenAI(response=fake_openai_response(analysis))
    provider = OpenAIProvider(client=client)
    analyze_with_llm(
        sample_page, page_signals, build_system(icp),
        AnalyzeSettings(provider="openai", model=None), provider=provider,
    )
    assert client.calls[0]["model"] == "gpt-5-mini"
