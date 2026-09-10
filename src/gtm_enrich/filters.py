"""One filter definition, compiled into whatever the target system speaks.

An ops person should be able to write "prospects with a website that I haven't
looked at in 90 days" once, and have it become a HubSpot search body or a SOQL
`WHERE` clause without knowing either. That is what this module does.

Two facts about HubSpot's search API drive the design, both verified against a
live portal rather than assumed:

* Filters **within** a `filterGroup` are ANDed; separate `filterGroups` are ORed.
  (Measured: one group with two filters returned 0 matches, the same two filters
  as separate groups returned 720,776.)
* There is therefore no native way to express "A AND (B OR C)". It has to be
  expanded into "(A AND B) OR (A AND C)" -- a cross product. `or_unknown` on a
  condition is exactly that case, and doubles the number of groups each time it
  is used, which is why the expansion is capped.

Salesforce has no such restriction; SOQL takes parentheses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from typing import Any, Literal

from .config import ConfigError, load_yaml

Op = Literal[
    "equals", "not_equals", "is_known", "is_unknown", "in", "not_in",
    "greater_than", "less_than", "contains", "older_than_days", "newer_than_days",
]

OPS: tuple[str, ...] = (
    "equals", "not_equals", "is_known", "is_unknown", "in", "not_in",
    "greater_than", "less_than", "contains", "older_than_days", "newer_than_days",
)

# Ops that take no value.
_VALUELESS = {"is_known", "is_unknown"}
# Ops whose value is a number of days, resolved to an absolute instant at compile time.
_RELATIVE_DATE = {"older_than_days", "newer_than_days"}

_HUBSPOT_OP = {
    "equals": "EQ", "not_equals": "NEQ", "is_known": "HAS_PROPERTY",
    "is_unknown": "NOT_HAS_PROPERTY", "in": "IN", "not_in": "NOT_IN",
    "greater_than": "GT", "less_than": "LT", "contains": "CONTAINS_TOKEN",
    "older_than_days": "LT", "newer_than_days": "GT",
}
_SOQL_OP = {
    "equals": "=", "not_equals": "!=", "in": "IN", "not_in": "NOT IN",
    "greater_than": ">", "less_than": "<",
    "older_than_days": "<", "newer_than_days": ">",
}

# `or_unknown` doubles the HubSpot group count each time it appears. Past this
# the query is almost certainly a mistake, and the error should say so rather
# than letting the API reject an enormous body.
MAX_FILTER_GROUPS = 16

_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class FilterError(ConfigError):
    """A filter definition is malformed or cannot be expressed for a system."""


@dataclass(frozen=True)
class FieldAlias:
    """One neutral field name and what it is called in each system."""

    hubspot: str | None = None
    salesforce: str | None = None

    def for_system(self, system: str) -> str | None:
        return getattr(self, system, None)


@dataclass(frozen=True)
class Condition:
    """One clause. `or_unknown` widens it to also match records with no value."""

    field: str
    op: str
    value: Any = None
    or_unknown: bool = False

    def __post_init__(self) -> None:
        if self.op not in OPS:
            raise FilterError(f"Unknown operator '{self.op}'. Known: {', '.join(OPS)}")
        if self.op in _VALUELESS and self.value is not None:
            raise FilterError(f"Operator '{self.op}' takes no value.")
        if self.op not in _VALUELESS and self.value is None:
            raise FilterError(f"Operator '{self.op}' requires a value.")
        if self.op in _RELATIVE_DATE and not isinstance(self.value, (int, float)):
            raise FilterError(f"Operator '{self.op}' takes a number of days.")


@dataclass(frozen=True)
class FilterSpec:
    """A whole filter file: field aliases, conditions, and escape hatches."""

    name: str
    fields: dict[str, FieldAlias] = field(default_factory=dict)
    conditions: list[Condition] = field(default_factory=list)
    limit: int = 200
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> FilterSpec:
        data = load_yaml(path)
        if "name" not in data:
            raise FilterError(f"{path} is missing required key: 'name'")

        fields: dict[str, FieldAlias] = {}
        for alias, targets in (data.get("fields") or {}).items():
            if not isinstance(targets, dict):
                raise FilterError(f"{path}: fields.{alias} must map system -> field name.")
            fields[str(alias)] = FieldAlias(
                hubspot=targets.get("hubspot"), salesforce=targets.get("salesforce")
            )

        conditions = []
        for i, raw_cond in enumerate(data.get("filters") or []):
            if not isinstance(raw_cond, dict) or "field" not in raw_cond:
                raise FilterError(f"{path}: filters[{i}] needs at least 'field' and 'op'.")
            conditions.append(
                Condition(
                    field=str(raw_cond["field"]),
                    op=str(raw_cond.get("op", "equals")),
                    value=raw_cond.get("value"),
                    or_unknown=bool(raw_cond.get("or_unknown", False)),
                )
            )

        return cls(
            name=str(data["name"]),
            fields=fields,
            conditions=conditions,
            limit=int(data.get("limit", 200)),
            raw=data.get("raw") or {},
        )

    def resolve(self, alias: str, system: str) -> str:
        """Neutral field name -> the name this system uses."""
        if alias in self.fields:
            resolved = self.fields[alias].for_system(system)
            if not resolved:
                raise FilterError(
                    f"Field '{alias}' has no '{system}' name. Add one under "
                    f"fields.{alias} in the filter file, or drop that filter."
                )
            return resolved
        # Not aliased: assume it is already a native field name.
        return alias


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _cutoff(days: float, now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(days=days)


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else [value]


# --------------------------------------------------------------------------- #
# HubSpot
# --------------------------------------------------------------------------- #


def _hubspot_filter(spec: FilterSpec, cond: Condition, now: datetime | None) -> dict[str, Any]:
    prop = spec.resolve(cond.field, "hubspot")
    out: dict[str, Any] = {"propertyName": prop, "operator": _HUBSPOT_OP[cond.op]}
    if cond.op in _VALUELESS:
        return out
    if cond.op in _RELATIVE_DATE:
        # HubSpot datetime properties are epoch milliseconds.
        out["value"] = int(_cutoff(cond.value, now).timestamp() * 1000)
    elif cond.op in ("in", "not_in"):
        out["values"] = [str(v) for v in _as_list(cond.value)]
    else:
        out["value"] = cond.value
    return out


def compile_hubspot(spec: FilterSpec, now: datetime | None = None) -> dict[str, Any]:
    """Build a HubSpot CRM search body.

    Every condition becomes one filter. A condition marked `or_unknown` becomes
    two alternatives, and the groups are the cross product of those alternatives,
    because HubSpot ORs groups and ANDs within them.
    """
    if "hubspot" in spec.raw:
        # Escape hatch: hand-written HubSpot JSON, used verbatim.
        body = dict(spec.raw["hubspot"])
        body.setdefault("limit", min(spec.limit, 100))
        return body

    alternatives: list[list[dict[str, Any]]] = []
    for cond in spec.conditions:
        primary = _hubspot_filter(spec, cond, now)
        if cond.or_unknown:
            prop = spec.resolve(cond.field, "hubspot")
            alternatives.append([primary, {"propertyName": prop, "operator": "NOT_HAS_PROPERTY"}])
        else:
            alternatives.append([primary])

    combos = 1
    for alt in alternatives:
        combos *= len(alt)
    if combos > MAX_FILTER_GROUPS:
        raise FilterError(
            f"This filter expands to {combos} HubSpot filter groups, over the "
            f"{MAX_FILTER_GROUPS} cap. Each 'or_unknown' doubles the count — use fewer, "
            "or write the query directly under raw.hubspot."
        )

    groups = [{"filters": list(combo)} for combo in product(*alternatives)] or [{"filters": []}]
    return {"filterGroups": groups, "limit": min(spec.limit, 100)}


# --------------------------------------------------------------------------- #
# Salesforce
# --------------------------------------------------------------------------- #


def _soql_literal(value: Any) -> str:
    """Quote and escape a value for SOQL. Numbers and booleans pass through."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _soql_field(name: str) -> str:
    if not _IDENTIFIER_RE.match(name):
        raise FilterError(f"Unsafe Salesforce field name in filter: {name!r}")
    return name


def _soql_clause(spec: FilterSpec, cond: Condition, now: datetime | None) -> str:
    fld = _soql_field(spec.resolve(cond.field, "salesforce"))

    if cond.op == "is_known":
        clause = f"{fld} != null"
    elif cond.op == "is_unknown":
        clause = f"{fld} = null"
    elif cond.op == "contains":
        needle = str(cond.value).replace("\\", "\\\\").replace("'", "\\'").replace("%", r"\%")
        clause = f"{fld} LIKE '%{needle}%'"
    elif cond.op in ("in", "not_in"):
        values = ", ".join(_soql_literal(v) for v in _as_list(cond.value))
        clause = f"{fld} {_SOQL_OP[cond.op]} ({values})"
    elif cond.op in _RELATIVE_DATE:
        # SOQL datetime literals are unquoted ISO-8601 with a Z offset.
        stamp = _cutoff(cond.value, now).strftime("%Y-%m-%dT%H:%M:%SZ")
        clause = f"{fld} {_SOQL_OP[cond.op]} {stamp}"
    else:
        clause = f"{fld} {_SOQL_OP[cond.op]} {_soql_literal(cond.value)}"

    if cond.or_unknown and cond.op not in ("is_known", "is_unknown"):
        clause = f"({clause} OR {fld} = null)"
    return clause


def compile_soql_where(spec: FilterSpec, now: datetime | None = None) -> str:
    """Build the WHERE clause. SOQL takes parentheses, so no expansion is needed."""
    if "salesforce" in spec.raw:
        return str(spec.raw["salesforce"].get("where", "")).strip()
    clauses = [_soql_clause(spec, c, now) for c in spec.conditions]
    return " AND ".join(clauses)


def compile_soql(
    spec: FilterSpec, object_type: str, fields: list[str], now: datetime | None = None
) -> str:
    """Build the full query, so `preview` can show exactly what will run."""
    selected = ", ".join(dict.fromkeys(_soql_field(f) for f in fields))
    query = f"SELECT {selected} FROM {_soql_field(object_type)}"
    where = compile_soql_where(spec, now)
    if where:
        query += f" WHERE {where}"
    return f"{query} LIMIT {int(spec.limit)}"
