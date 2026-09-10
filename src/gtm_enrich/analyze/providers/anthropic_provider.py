"""Anthropic (Claude) provider.

Uses `messages.parse` with the Pydantic model as the output format, so the
response is schema-valid by construction rather than by hope. Two Anthropic
specifics are handled here and nowhere else: the ephemeral cache breakpoint on
the system block, and server-side refusal fallbacks.
"""

from __future__ import annotations

import logging

from ...models import HomepageAnalysis
from .base import AnalysisError, LLMProvider, LLMResult, ProviderNotConfigured

log = logging.getLogger(__name__)

# The scalar `fallbacks="default"` form requires this header; the older array
# form uses -2026-06-01. Pairing either header with the other form is a 400.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    default_model = "claude-opus-5"

    def __init__(self, client=None, *, enable_fallbacks: bool = True) -> None:
        self.enable_fallbacks = enable_fallbacks
        self._client = client or self._build_client()

    @staticmethod
    def _build_client():
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise ProviderNotConfigured(
                "The `anthropic` package is not installed. `pip install anthropic`, "
                "choose another provider, or run with --no-llm."
            ) from exc
        # A bare client resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an
        # `ant auth login` profile on disk -- don't second-guess it here.
        return anthropic.Anthropic()

    def analyze(
        self, system: str, user: str, model: str, effort: str, max_tokens: int
    ) -> LLMResult:
        request: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
            "output_format": HomepageAnalysis,
            "system": [
                {
                    "type": "text",
                    "text": system,
                    # Identical for every domain in a run: this is the cached prefix.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user}],
        }
        if self.enable_fallbacks:
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"

        response = self._call(request)

        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            category = getattr(detail, "category", None) if detail else None
            raise AnalysisError(f"Model declined to analyze this page (category: {category}).")

        analysis = response.parsed_output
        if analysis is None:
            raise AnalysisError(f"No parsed output returned (stop_reason={response.stop_reason}).")

        usage = response.usage
        return LLMResult(
            analysis=analysis,
            model=getattr(response, "model", model),
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cached_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )

    def _call(self, request: dict):
        """Issue the request, retrying once without the fallback beta if rejected.

        The beta is worth having -- it rescues a refused request inside the same
        call -- but it should never be why a whole enrichment run fails.
        """
        import anthropic

        try:
            return self._client.beta.messages.parse(**request)
        except anthropic.BadRequestError as exc:
            if not self.enable_fallbacks or "fallback" not in str(exc).lower():
                raise AnalysisError(f"Anthropic API rejected the request: {exc}") from exc
            log.warning("Server-side fallbacks unavailable, retrying without them: %s", exc)
            retry = {k: v for k, v in request.items() if k not in ("betas", "fallbacks")}
            try:
                return self._client.beta.messages.parse(**retry)
            except anthropic.APIError as retry_exc:
                raise AnalysisError(f"Anthropic API error: {retry_exc}") from retry_exc
        except anthropic.APIStatusError as exc:
            raise AnalysisError(f"Anthropic API error {exc.status_code}: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise AnalysisError(f"Could not reach the Anthropic API: {exc}") from exc
