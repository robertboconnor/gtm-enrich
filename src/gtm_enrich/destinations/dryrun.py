"""The destination that ships working with no credentials at all.

It builds exactly the payload a real destination would receive -- same mapping,
same coercion, same field names -- and writes it to disk instead of sending it.
That makes it the thing you run before a real load, not just a demo mode.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ..models import CrmRecord, EnrichmentResult, WriteResult, utcnow
from .base import Destination


class DryRunDestination(Destination):
    """Writes the payloads it *would* send to `out/`, shaped for a real destination.

    `shape` picks whose field names to use, so `--dest dryrun --shape salesforce`
    previews the exact Salesforce payload without touching an org.
    """

    def __init__(self, output_dir: Path, shape: str = "hubspot") -> None:
        self.name = f"dryrun:{shape}"
        self.shape = shape
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._payloads: list[dict[str, Any]] = []

    def find(self, record: CrmRecord) -> tuple[str, dict[str, Any]] | None:
        return None  # nothing exists in a dry run

    def create(self, record: CrmRecord) -> str:  # pragma: no cover - upsert is overridden
        raise NotImplementedError

    def update(self, record_id: str, properties: dict[str, Any]) -> None:  # pragma: no cover
        raise NotImplementedError

    def upsert(self, result: EnrichmentResult, record: CrmRecord) -> WriteResult:
        self._payloads.append(
            {
                "domain": result.domain,
                "object_type": record.object_type,
                "match_key": record.match_key,
                "match_value": record.match_value,
                "properties": record.properties,
            }
        )
        return WriteResult(
            domain=result.domain,
            destination=self.name,
            action="would_create",
            changed_fields=sorted(record.properties),
        )

    def flush(self) -> dict[str, Path]:
        """Write `payloads.json` and a flat `records.csv`, and return both paths.

        Clears the buffer, so calling this and then closing does not write twice.
        """
        stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
        json_path = self.output_dir / f"payloads-{self.shape}-{stamp}.json"
        json_path.write_text(json.dumps(self._payloads, indent=2, default=str), encoding="utf-8")

        csv_path = self.output_dir / f"records-{self.shape}-{stamp}.csv"
        columns: list[str] = []
        for payload in self._payloads:
            for key in payload["properties"]:
                if key not in columns:
                    columns.append(key)

        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["domain", *columns], extrasaction="ignore")
            writer.writeheader()
            for payload in self._payloads:
                writer.writerow({"domain": payload["domain"], **payload["properties"]})

        self._payloads.clear()
        return {"json": json_path, "csv": csv_path}

    def close(self) -> None:
        if self._payloads:
            self.flush()
