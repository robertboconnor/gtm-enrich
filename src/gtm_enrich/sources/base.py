"""Where the list of accounts comes from.

The mirror image of `Destination`. A destination knows how to find and write one
record; a source knows how to ask "which records match this?" and hand back a
list of domains. Same credentials, same clients, opposite direction.

Keeping them separate matters for one reason: you can read from one system and
write to another. Pull the target list out of Salesforce, enrich, and write into
HubSpot -- which is a real thing ops teams need and a single combined
"CRM connector" abstraction would make awkward.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..filters import FilterSpec


class SourceError(RuntimeError):
    """A source could not be configured or queried."""


class SourceNotConfigured(SourceError):
    """The source is missing credentials."""


@dataclass
class SourceRecord:
    """One account to enrich, and where it came from."""

    domain: str
    record_id: str | None = None
    # The fields we asked the source for. Carried rather than discarded because
    # a source that already returned the enrichment fields has told us the
    # record's current values -- enough to skip a read on the write side later.
    properties: dict[str, Any] = field(default_factory=dict)
    source: str = ""


class Source(ABC):
    """Produces the list of domains to enrich."""

    name: str = "base"

    @abstractmethod
    def describe(self, spec: FilterSpec) -> str:
        """The exact query this will run, for `preview` to print before running it."""

    @abstractmethod
    def fetch(self, spec: FilterSpec, limit: int | None = None) -> list[SourceRecord]:
        """Return matching records, paging as needed."""

    def preflight(self, spec: FilterSpec) -> list[str]:
        """Problems to report before querying. Empty means good to go."""
        return []

    def close(self) -> None:  # noqa: B027 - optional; not every source holds one
        """Release any held connections."""

    def __enter__(self) -> Source:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
