"""Source registry. Add a module here and `--source <name>` works."""

from __future__ import annotations

from pathlib import Path

from .base import Source, SourceError, SourceNotConfigured, SourceRecord
from .csvfile import CsvSource
from .hubspot import HubSpotSource
from .salesforce import SalesforceSource

SOURCES = ("csv", "hubspot", "salesforce")

SOURCE_REQUIREMENTS: dict[str, str] = {
    "csv": "a file path — nothing else",
    "hubspot": "HUBSPOT_PRIVATE_APP_TOKEN (company read scope)",
    "salesforce": "SF_ACCESS_TOKEN or SF_CLIENT_ID",
}


def build_source(
    name: str,
    *,
    path: Path | None = None,
    object_type: str | None = None,
    domain_field: str | None = None,
    extra_properties: list[str] | None = None,
) -> Source:
    if name == "csv":
        if path is None:
            raise SourceError("The csv source needs a file path (--domains).")
        return CsvSource(path)
    if name == "hubspot":
        return HubSpotSource(
            object_type=object_type or "companies",
            domain_property=domain_field or "domain",
            extra_properties=extra_properties,
        )
    if name == "salesforce":
        return SalesforceSource(
            object_type=object_type or "Account",
            domain_field=domain_field or "Website",
            extra_fields=extra_properties,
        )
    raise SourceError(f"Unknown source '{name}'. Known: {', '.join(SOURCES)}")


__all__ = [
    "CsvSource", "HubSpotSource", "SalesforceSource", "SOURCES", "SOURCE_REQUIREMENTS",
    "Source", "SourceError", "SourceNotConfigured", "SourceRecord", "build_source",
]
