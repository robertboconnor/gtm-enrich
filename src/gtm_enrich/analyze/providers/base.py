"""The LLM provider contract.

One method, one return type. Everything provider-specific -- how structured
output is requested, what the usage object is called, which errors mean "retry"
-- stays behind this line, so `pipeline.py` never learns which vendor is in play.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ...models import HomepageAnalysis


class AnalysisError(RuntimeError):
    """The model could not produce an analysis for this page."""


class ProviderNotConfigured(AnalysisError):
    """The provider's SDK or credentials are missing."""


@dataclass
class LLMResult:
    """What every provider returns, whatever its SDK calls these things."""

    analysis: HomepageAnalysis
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    @property
    def billed_input_tokens(self) -> int:
        """Total tokens read in, cache hits included."""
        return self.input_tokens + self.cached_input_tokens


class LLMProvider(ABC):
    """Turns a system + user prompt into a validated `HomepageAnalysis`."""

    name: str = "base"
    default_model: str = ""

    @abstractmethod
    def analyze(
        self, system: str, user: str, model: str, effort: str, max_tokens: int
    ) -> LLMResult:
        """Run one page. Raise `AnalysisError` on refusal, API failure, or bad output."""

    def resolve_model(self, model: str | None) -> str:
        return model or self.default_model
