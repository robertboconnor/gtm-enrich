"""Configuration: environment for secrets, YAML for the parts ops teams edit.

Secrets and endpoints come from the environment. The ICP definition and the
field mapping live in `config/*.yaml` on purpose -- those are the two things a
RevOps person needs to change without touching Python.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"

# Priced per 1M tokens, from the Anthropic pricing page. Used only for the
# cost estimate printed in the run summary.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

DEFAULT_MODEL = "claude-opus-5"


class ConfigError(RuntimeError):
    """Raised when config on disk is missing or malformed."""


@dataclass(frozen=True)
class ScrapeSettings:
    user_agent: str = (
        "gtm-enrich/0.1 (+https://github.com/robertboconnor/gtm-enrich; "
        "homepage enrichment POC)"
    )
    timeout_seconds: float = 20.0
    max_redirects: int = 5
    concurrency: int = 5
    respect_robots: bool = True
    max_markdown_chars: int = 40_000
    cache_dir: Path = PROJECT_ROOT / ".cache" / "pages"
    cache_ttl_hours: int = 24 * 7


@dataclass(frozen=True)
class AnalyzeSettings:
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    max_tokens: int = 8_000
    # Server-side refusal fallback: on a policy decline the API retries the same
    # request on a fallback model inside the same call instead of returning nothing.
    enable_fallbacks: bool = True


@dataclass
class Settings:
    scrape: ScrapeSettings = field(default_factory=ScrapeSettings)
    analyze: AnalyzeSettings = field(default_factory=AnalyzeSettings)
    config_dir: Path = DEFAULT_CONFIG_DIR
    output_dir: Path = PROJECT_ROOT / "out"

    @classmethod
    def from_env(cls) -> Settings:
        def _f(name: str, default: float) -> float:
            raw = os.getenv(name)
            return float(raw) if raw else default

        def _i(name: str, default: int) -> int:
            raw = os.getenv(name)
            return int(raw) if raw else default

        return cls(
            scrape=ScrapeSettings(
                timeout_seconds=_f("GTM_SCRAPE_TIMEOUT", 20.0),
                concurrency=_i("GTM_SCRAPE_CONCURRENCY", 5),
                respect_robots=os.getenv("GTM_RESPECT_ROBOTS", "1") != "0",
            ),
            analyze=AnalyzeSettings(
                model=os.getenv("GTM_MODEL", DEFAULT_MODEL),
                effort=os.getenv("GTM_EFFORT", "medium"),
                enable_fallbacks=os.getenv("GTM_ENABLE_FALLBACKS", "1") != "0",
            ),
            config_dir=Path(os.getenv("GTM_CONFIG_DIR", str(DEFAULT_CONFIG_DIR))),
            output_dir=Path(os.getenv("GTM_OUTPUT_DIR", str(PROJECT_ROOT / "out"))),
        )


def has_anthropic_credentials() -> bool:
    """True when the Anthropic SDK will find a credential without being handed one.

    An unset ANTHROPIC_API_KEY does not mean there are no credentials -- the SDK
    also reads ANTHROPIC_AUTH_TOKEN and an `ant auth login` profile on disk.
    """
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return True
    profile_dir = Path.home() / ".config" / "anthropic"
    return profile_dir.is_dir() and any(profile_dir.glob("*.json"))


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
    return data


@dataclass(frozen=True)
class IcpProfile:
    """The ICP definition, serving two consumers with different needs.

    `description` / `good_fit` / `poor_fit` are prose: they go into the prompt,
    where a model can reason about them. `heuristic_good` / `heuristic_poor` are
    literal strings the keyword fallback greps for. Keeping them separate is the
    point -- prose bullets do not appear verbatim on a homepage, so deriving the
    fallback's terms from the prompt copy produced junk matches.
    """

    name: str
    description: str
    good_fit: list[str]
    poor_fit: list[str]
    scoring_notes: str = ""
    heuristic_good: list[str] = field(default_factory=list)
    heuristic_poor: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> IcpProfile:
        data = load_yaml(path)
        missing = {"name", "description"} - set(data)
        if missing:
            raise ConfigError(f"{path} is missing required key(s): {sorted(missing)}")
        return cls(
            name=str(data["name"]),
            description=str(data["description"]).strip(),
            good_fit=[str(x) for x in data.get("good_fit", [])],
            poor_fit=[str(x) for x in data.get("poor_fit", [])],
            scoring_notes=str(data.get("scoring_notes", "")).strip(),
            heuristic_good=[str(x).lower() for x in
                            data.get("heuristic_keywords", {}).get("good", [])],
            heuristic_poor=[str(x).lower() for x in
                            data.get("heuristic_keywords", {}).get("poor", [])],
        )

    def as_prompt_block(self) -> str:
        lines = [f"ICP: {self.name}", "", self.description]
        if self.good_fit:
            lines += ["", "Strong fit signals:"] + [f"- {s}" for s in self.good_fit]
        if self.poor_fit:
            lines += ["", "Poor fit signals:"] + [f"- {s}" for s in self.poor_fit]
        if self.scoring_notes:
            lines += ["", "Scoring guidance:", self.scoring_notes]
        return "\n".join(lines)


@dataclass(frozen=True)
class FieldMapping:
    """Maps one enrichment field onto its API name in each destination."""

    source: str
    targets: dict[str, str]
    value_type: str = "string"


@dataclass(frozen=True)
class MappingConfig:
    """The full mapping doc: object names per destination plus the field table."""

    objects: dict[str, str]
    match_keys: dict[str, str]
    fields: list[FieldMapping]

    @classmethod
    def load(cls, path: Path) -> MappingConfig:
        data = load_yaml(path)
        for key in ("objects", "match_keys", "fields"):
            if key not in data:
                raise ConfigError(f"{path} is missing required key: '{key}'")
        if not isinstance(data["fields"], list):
            raise ConfigError(f"{path}: 'fields' must be a list.")

        fields_out: list[FieldMapping] = []
        for i, raw in enumerate(data["fields"]):
            if not isinstance(raw, dict) or "source" not in raw:
                raise ConfigError(f"{path}: fields[{i}] must be a mapping with a 'source' key.")
            targets = {
                k: str(v)
                for k, v in raw.items()
                if k not in {"source", "type"} and v is not None
            }
            fields_out.append(
                FieldMapping(
                    source=str(raw["source"]),
                    targets=targets,
                    value_type=str(raw.get("type", "string")),
                )
            )
        return cls(
            objects={k: str(v) for k, v in data["objects"].items()},
            match_keys={k: str(v) for k, v in data["match_keys"].items()},
            fields=fields_out,
        )

    def for_destination(self, destination: str) -> list[FieldMapping]:
        """Only the fields that have a target column for this destination."""
        return [f for f in self.fields if destination in f.targets]


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    prices = MODEL_PRICING.get(model)
    if prices is None:
        return None
    in_rate, out_rate = prices
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate
