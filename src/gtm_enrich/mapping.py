"""Turn an `EnrichmentResult` into a destination-shaped payload.

The only place in the codebase that knows enrichment fields have CRM names, and
it learns that from YAML rather than from code. Type coercion lives here too:
a list is a semicolon-joined string in Salesforce, HubSpot wants "true"/"false"
strings for checkboxes, and both want ISO-8601 for dates.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from .config import FieldMapping, MappingConfig
from .models import CrmRecord, EnrichmentResult


class MappingError(RuntimeError):
    """A mapping entry points at a field the enrichment result does not have."""


def resolve_path(result: EnrichmentResult, path: str) -> Any:
    """Walk a dotted path like 'analysis.icp_fit_score' into the result object."""
    current: Any = result
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, BaseModel):
            if not hasattr(current, part):
                raise MappingError(f"'{path}': no field '{part}' on {type(current).__name__}")
            current = getattr(current, part)
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            raise MappingError(f"'{path}': cannot descend into {type(current).__name__}")
    return current


def coerce(value: Any, value_type: str, destination: str) -> Any:
    """Render a Python value the way the destination's API expects it."""
    if value is None:
        return None

    if value_type == "list":
        items = [str(v) for v in value] if isinstance(value, (list, tuple)) else [str(value)]
        return "; ".join(items) if items else None

    if value_type == "boolean":
        # HubSpot checkbox properties take the strings "true"/"false";
        # Salesforce takes a real JSON boolean.
        return str(bool(value)).lower() if destination == "hubspot" else bool(value)

    if value_type == "number":
        return value if isinstance(value, (int, float)) else float(value)

    if value_type == "datetime":
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    text = str(value)
    return text if text.strip() else None


def build_record(
    result: EnrichmentResult, mapping: MappingConfig, destination: str
) -> CrmRecord:
    """Map one enrichment result onto one destination's object shape."""
    if destination not in mapping.objects:
        raise MappingError(
            f"No object configured for destination '{destination}'. "
            f"Known: {sorted(mapping.objects)}"
        )
    if not result.ok:
        raise MappingError(f"Refusing to map a failed enrichment for {result.domain}.")

    properties: dict[str, Any] = {}
    for field in mapping.for_destination(destination):
        raw = resolve_path(result, field.source)
        value = coerce(raw, field.value_type, destination)
        if value is not None:
            properties[field.targets[destination]] = value

    match_key = mapping.match_keys.get(destination)
    if not match_key:
        raise MappingError(f"No match_key configured for destination '{destination}'.")

    # Salesforce matches on Website, which needs a URL, not a bare domain.
    match_value = (
        f"https://{result.domain}" if match_key.lower() == "website" else result.domain
    )
    # Make sure the match field is on the payload for creates.
    properties.setdefault(match_key, match_value)

    return CrmRecord(
        object_type=mapping.objects[destination],
        match_key=match_key,
        match_value=match_value,
        properties=properties,
    )


def diff_properties(
    existing: dict[str, Any], proposed: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Fields whose value would actually change, so we never write a no-op.

    Every value is compared as a trimmed string: CRMs round-trip numbers and
    booleans inconsistently, and a 78 that comes back as "78" is not a change.
    """

    def norm(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, bool):
            return str(v).lower()
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v).strip()

    changes = {k: v for k, v in proposed.items() if norm(existing.get(k)) != norm(v)}
    return changes, sorted(changes)


def mapping_field_names(mapping: MappingConfig, destination: str) -> list[str]:
    """Every API field name this destination writes -- used to fetch current values."""
    return [f.targets[destination] for f in mapping.for_destination(destination)]


__all__ = [
    "build_record",
    "coerce",
    "diff_properties",
    "MappingError",
    "mapping_field_names",
    "resolve_path",
    "FieldMapping",
]
