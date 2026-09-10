"""The judgement step: homepage markdown -> a schema-valid `HomepageAnalysis`.

Structured outputs do the heavy lifting. The model is handed the pydantic model
as a JSON schema and cannot return anything that fails to parse, which is what
makes it safe to write the result into a CRM without a human in the loop.
"""

from __future__ import annotations

import logging

from ..config import AnalyzeSettings, estimate_cost_usd
from ..models import HomepageAnalysis, Provenance, ScrapedPage, utcnow
from .prompt import build_system, build_user

log = logging.getLogger(__name__)

# The array form of server-side fallbacks uses -2026-06-01; the scalar
# `fallbacks="default"` form requires this header instead.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnalysisError(RuntimeError):
    """The model could not produce an analysis for this page."""


def get_client():
    """Import the SDK lazily so heuristic-only runs need no `anthropic` install."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise AnalysisError(
            "The `anthropic` package is not installed. Install it, or run with --no-llm."
        ) from exc
    return anthropic.Anthropic()


def analyze_with_llm(
    page: ScrapedPage,
    page_signals: dict[str, bool],
    system_prompt: str,
    settings: AnalyzeSettings,
    client=None,
) -> tuple[HomepageAnalysis, Provenance]:
    """Run one page through the model. Raises `AnalysisError` on refusal or API failure."""
    client = client or get_client()

    request: dict = {
        "model": settings.model,
        "max_tokens": settings.max_tokens,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": settings.effort},
        "output_format": HomepageAnalysis,
        "system": [
            {
                "type": "text",
                "text": system_prompt,
                # Stable across every domain in the run -- this is the cached prefix.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": build_user(page, page_signals)}],
    }
    if settings.enable_fallbacks:
        request["betas"] = [FALLBACK_BETA]
        request["fallbacks"] = "default"

    response = _call(client, request, settings)

    if response.stop_reason == "refusal":
        detail = getattr(response, "stop_details", None)
        category = getattr(detail, "category", None) if detail else None
        raise AnalysisError(f"Model declined to analyze this page (category: {category}).")

    analysis = response.parsed_output
    if analysis is None:
        raise AnalysisError(f"No parsed output returned (stop_reason={response.stop_reason}).")

    usage = response.usage
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0

    provenance = Provenance(
        source_url=page.final_url,
        scraped_at=page.fetched_at,
        analyzed_at=utcnow(),
        analyzer=f"llm:{getattr(response, 'model', settings.model)}",
        content_hash=page.content_hash,
        input_tokens=input_tokens + cached,
        output_tokens=output_tokens,
        cost_usd=estimate_cost_usd(settings.model, input_tokens + cached, output_tokens),
    )
    return analysis, provenance


def _call(client, request: dict, settings: AnalyzeSettings):
    """Issue the request, retrying once without the fallback beta if it is rejected.

    The beta is worth having -- it rescues a refused request inside the same call --
    but it should never be the reason a whole enrichment run fails.
    """
    import anthropic

    try:
        return client.beta.messages.parse(**request)
    except anthropic.BadRequestError as exc:
        if not settings.enable_fallbacks or "fallback" not in str(exc).lower():
            raise AnalysisError(f"Anthropic API rejected the request: {exc}") from exc
        log.warning("Server-side fallbacks unavailable, retrying without them: %s", exc)
        retry = {k: v for k, v in request.items() if k not in ("betas", "fallbacks")}
        try:
            return client.beta.messages.parse(**retry)
        except anthropic.APIError as retry_exc:
            raise AnalysisError(f"Anthropic API error: {retry_exc}") from retry_exc
    except anthropic.APIStatusError as exc:
        raise AnalysisError(f"Anthropic API error {exc.status_code}: {exc}") from exc
    except anthropic.APIConnectionError as exc:
        raise AnalysisError(f"Could not reach the Anthropic API: {exc}") from exc


__all__ = ["analyze_with_llm", "AnalysisError", "build_system"]
