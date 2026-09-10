"""Provider registry. Add a module here and `--provider <name>` works."""

from __future__ import annotations

from .anthropic_provider import AnthropicProvider
from .base import AnalysisError, LLMProvider, LLMResult, ProviderNotConfigured
from .openai_provider import OpenAIProvider

PROVIDERS: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}

PROVIDER_NAMES = tuple(PROVIDERS)


def build_provider(name: str, *, enable_fallbacks: bool = True, client=None) -> LLMProvider:
    """Instantiate a provider by name."""
    try:
        cls = PROVIDERS[name]
    except KeyError:
        raise ProviderNotConfigured(
            f"Unknown LLM provider '{name}'. Known: {', '.join(PROVIDER_NAMES)}"
        ) from None
    if cls is AnthropicProvider:
        return cls(client=client, enable_fallbacks=enable_fallbacks)
    return cls(client=client)


def default_model_for(name: str) -> str:
    cls = PROVIDERS.get(name)
    return cls.default_model if cls else ""


__all__ = [
    "AnalysisError",
    "AnthropicProvider",
    "LLMProvider",
    "LLMResult",
    "OpenAIProvider",
    "PROVIDERS",
    "PROVIDER_NAMES",
    "ProviderNotConfigured",
    "build_provider",
    "default_model_for",
]
