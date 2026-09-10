"""OpenAI (GPT) provider.

Uses the Responses API's `parse` with `text_format`, OpenAI's equivalent of
Anthropic's `output_format`: the same Pydantic model, the same guarantee that a
malformed record is impossible. Reasoning effort maps onto `reasoning.effort`.
"""

from __future__ import annotations

import logging

from ...models import HomepageAnalysis
from .base import AnalysisError, LLMProvider, LLMResult, ProviderNotConfigured

log = logging.getLogger(__name__)

# Anthropic exposes five effort levels; OpenAI's reasoning effort has four.
# "xhigh" and "max" both land on "high" rather than erroring, so a single
# --effort flag works across providers.
EFFORT_MAP = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
    "minimal": "minimal",
}


class OpenAIProvider(LLMProvider):
    name = "openai"
    default_model = "gpt-5-mini"

    def __init__(self, client=None) -> None:
        self._client = client or self._build_client()

    @staticmethod
    def _build_client():
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise ProviderNotConfigured(
                "The `openai` package is not installed. `pip install openai`, "
                "choose another provider, or run with --no-llm."
            ) from exc
        return openai.OpenAI()

    def analyze(
        self, system: str, user: str, model: str, effort: str, max_tokens: int
    ) -> LLMResult:
        import openai

        try:
            response = self._client.responses.parse(
                model=model,
                # The Responses API has no separate system field; an input message
                # with role "system" is the equivalent, and keeping it first keeps
                # it in the automatically-cached prefix.
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                text_format=HomepageAnalysis,
                max_output_tokens=max_tokens,
                reasoning={"effort": EFFORT_MAP.get(effort, "medium")},
            )
        except openai.BadRequestError as exc:
            raise AnalysisError(f"OpenAI API rejected the request: {exc}") from exc
        except openai.APIStatusError as exc:
            raise AnalysisError(f"OpenAI API error {exc.status_code}: {exc}") from exc
        except openai.APIConnectionError as exc:
            raise AnalysisError(f"Could not reach the OpenAI API: {exc}") from exc

        # A run that hits the output cap or is refused comes back "incomplete"
        # with no parsed object, rather than raising.
        if response.status == "incomplete":
            reason = getattr(response.incomplete_details, "reason", "unknown")
            raise AnalysisError(f"OpenAI returned an incomplete response ({reason}).")

        analysis = response.output_parsed
        if analysis is None:
            raise AnalysisError(f"No parsed output returned (status={response.status}).")

        usage = response.usage
        cached = 0
        if usage is not None and usage.input_tokens_details is not None:
            cached = getattr(usage.input_tokens_details, "cached_tokens", 0) or 0

        total_input = (getattr(usage, "input_tokens", 0) or 0) if usage else 0
        return LLMResult(
            analysis=analysis,
            model=getattr(response, "model", model),
            # OpenAI reports cached tokens *inside* input_tokens; Anthropic
            # reports them alongside. Normalize to Anthropic's split so
            # `billed_input_tokens` means the same thing for both.
            input_tokens=max(0, total_input - cached),
            output_tokens=(getattr(usage, "output_tokens", 0) or 0) if usage else 0,
            cached_input_tokens=cached,
        )
