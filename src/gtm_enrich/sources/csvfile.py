"""A CSV or text file as a source.

The original way in, expressed through the same interface as the CRM sources so
the rest of the pipeline stops caring where a list came from. Filters do not
apply -- the file is the filter.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ..filters import FilterSpec
from .base import Source, SourceError, SourceRecord

_DOMAIN_COLUMNS = ("domain", "website", "company_domain", "url")


class CsvSource(Source):
    name = "csv"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise SourceError(f"No such file: {self.path}")

    def describe(self, spec: FilterSpec) -> str:
        return f"read every row of {self.path} (filters do not apply to files)"

    def fetch(self, spec: FilterSpec, limit: int | None = None) -> list[SourceRecord]:
        text = self.path.read_text(encoding="utf-8")

        if self.path.suffix.lower() != ".csv":
            values = [
                ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")
            ]
        else:
            rows = list(csv.reader(text.splitlines()))
            if not rows:
                return []
            header = [h.strip().lower() for h in rows[0]]
            column = next((c for c in _DOMAIN_COLUMNS if c in header), None)
            if column:
                idx = header.index(column)
                values = [r[idx].strip() for r in rows[1:] if len(r) > idx and r[idx].strip()]
            else:
                # No recognizable header: assume the first column, keeping row 1
                # only if it doesn't look like a header.
                start = 0 if rows[0] and "." in rows[0][0] else 1
                values = [r[0].strip() for r in rows[start:] if r and r[0].strip()]

        if limit:
            values = values[:limit]
        return [SourceRecord(domain=v, source=self.name) for v in values]
