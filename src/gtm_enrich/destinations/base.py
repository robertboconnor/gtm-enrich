"""The destination contract.

Every destination answers three questions -- does this record exist, how do I
create it, how do I update it -- and the shared `upsert` turns those into a
find/diff/write cycle. That means idempotency and no-op suppression are written
once and every destination gets them, including ones added later.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from ..mapping import diff_properties
from ..models import CrmRecord, EnrichmentResult, WriteResult

log = logging.getLogger(__name__)


class DestinationError(RuntimeError):
    """A destination could not be configured or reached."""


class Destination(ABC):
    """A GTM system of record we can write enrichment into."""

    name: str = "base"

    @abstractmethod
    def find(self, record: CrmRecord) -> tuple[str, dict[str, Any]] | None:
        """Return `(record_id, current_properties)`, or None if no match exists."""

    @abstractmethod
    def create(self, record: CrmRecord) -> str:
        """Create the record and return its id."""

    @abstractmethod
    def update(self, record_id: str, properties: dict[str, Any]) -> None:
        """Patch the given properties onto an existing record."""

    def upsert(self, result: EnrichmentResult, record: CrmRecord) -> WriteResult:
        """Find, diff, then write only what changed."""
        try:
            found = self.find(record)

            if found is None:
                record_id = self.create(record)
                return WriteResult(
                    domain=result.domain,
                    destination=self.name,
                    action="created",
                    record_id=record_id,
                    changed_fields=sorted(record.properties),
                )

            record_id, existing = found
            changes, changed_fields = diff_properties(existing, record.properties)
            if not changes:
                return WriteResult(
                    domain=result.domain,
                    destination=self.name,
                    action="skipped",
                    record_id=record_id,
                )

            self.update(record_id, changes)
            return WriteResult(
                domain=result.domain,
                destination=self.name,
                action="updated",
                record_id=record_id,
                changed_fields=changed_fields,
            )

        except Exception as exc:  # one bad account must not sink the batch
            log.warning("write failed for %s: %s", result.domain, exc)
            return WriteResult(
                domain=result.domain,
                destination=self.name,
                action="failed",
                error=str(exc),
            )

    def close(self) -> None:  # noqa: B027 - optional hook, not every destination holds one
        """Release any held connections."""

    def __enter__(self) -> Destination:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
