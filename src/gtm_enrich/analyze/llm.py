"""Provider-agnostic analysis entry point.

`pipeline.py` calls this and never learns which vendor answered. Everything
below is bookkeeping: resolve the model, run the provider, record provenance.
"""

from __future__ import annotations

import logging

from ..config import AnalyzeSettings, estimate_cost_usd
from ..models import HomepageAnalysis, Provenance, ScrapedPage, utcnow
from .prompt import build_system, build_user
from .providers import (
    AnalysisError,
    LLMProvider,
    ProviderNotConfigured,
    build_provider,
)

log = logging.getLogger(__name__)


def get_provider(settings: AnalyzeSettings, client=None) -> LLMProvider:
    """Build the configured provider. Raises `ProviderNotConfigured` if unavailable."""
    return build_provider(
        settings.provider, enable_fallbacks=settings.enable_fallbacks, client=client
    )


def analyze_with_llm(
    page: ScrapedPage,
    page_signals: dict[str, bool],
    system_prompt: str,
    settings: AnalyzeSettings,
    provider: LLMProvider | None = None,
) -> tuple[HomepageAnalysis, Provenance]:
    """Run one page through the configured model. Raises `AnalysisError` on failure."""
    provider = provider or get_provider(settings)
    model = provider.resolve_model(settings.model)

    result = provider.analyze(
        system=system_prompt,
        user=build_user(page, page_signals),
        model=model,
        effort=settings.effort,
        max_tokens=settings.max_tokens,
    )

    provenance = Provenance(
        source_url=page.final_url,
        scraped_at=page.fetched_at,
        analyzed_at=utcnow(),
        analyzer=f"{provider.name}:{result.model}",
        content_hash=page.content_hash,
        input_tokens=result.billed_input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=estimate_cost_usd(result.model, result.billed_input_tokens, result.output_tokens),
    )
    return result.analysis, provenance


__all__ = [
    "AnalysisError",
    "ProviderNotConfigured",
    "analyze_with_llm",
    "build_system",
    "get_provider",
]
