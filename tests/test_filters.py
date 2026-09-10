"""Filter compilation: one definition, two very different query languages."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from gtm_enrich.filters import (
    MAX_FILTER_GROUPS,
    Condition,
    FieldAlias,
    FilterError,
    FilterSpec,
    compile_hubspot,
    compile_soql,
    compile_soql_where,
)

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parents[1]

FIELDS = {
    "website": FieldAlias(hubspot="domain", salesforce="Website"),
    "lifecycle": FieldAlias(hubspot="lifecyclestage", salesforce="Type"),
    "enriched_at": FieldAlias(hubspot="gtm_enriched_at", salesforce="GTM_Enriched_At__c"),
}


def spec(*conditions: Condition, **kwargs) -> FilterSpec:
    return FilterSpec(name="t", fields=FIELDS, conditions=list(conditions), **kwargs)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def test_shipped_filter_file_loads() -> None:
    loaded = FilterSpec.load(REPO_ROOT / "config" / "filters" / "new-prospects.yaml")
    assert loaded.conditions
    assert loaded.resolve("website", "hubspot") == "domain"
    assert loaded.resolve("website", "salesforce") == "Website"


def test_unknown_operator_is_rejected() -> None:
    with pytest.raises(FilterError, match="Unknown operator"):
        Condition("website", "sort_of_equals", "x")


def test_valueless_operators_reject_a_value() -> None:
    with pytest.raises(FilterError, match="takes no value"):
        Condition("website", "is_known", "x")


def test_value_operators_require_one() -> None:
    with pytest.raises(FilterError, match="requires a value"):
        Condition("website", "equals")


def test_unaliased_fields_pass_through_as_native_names() -> None:
    assert spec().resolve("num_associated_contacts", "hubspot") == "num_associated_contacts"


def test_a_field_with_no_name_for_this_system_is_a_clear_error() -> None:
    s = FilterSpec(name="t", fields={"x": FieldAlias(hubspot="x_hs")})
    with pytest.raises(FilterError, match="no 'salesforce' name"):
        s.resolve("x", "salesforce")


# --------------------------------------------------------------------------- #
# HubSpot
# --------------------------------------------------------------------------- #


def test_conditions_become_one_anded_group() -> None:
    body = compile_hubspot(
        spec(Condition("website", "is_known"), Condition("lifecycle", "not_equals", "customer"))
    )
    assert len(body["filterGroups"]) == 1
    assert body["filterGroups"][0]["filters"] == [
        {"propertyName": "domain", "operator": "HAS_PROPERTY"},
        {"propertyName": "lifecyclestage", "operator": "NEQ", "value": "customer"},
    ]


def test_or_unknown_expands_into_a_second_group() -> None:
    """HubSpot ANDs within a group and ORs between them, so 'A AND (B OR C)'
    has to become '(A AND B) OR (A AND C)'. Verified against a live portal."""
    body = compile_hubspot(
        spec(
            Condition("website", "is_known"),
            Condition("enriched_at", "older_than_days", 90, or_unknown=True),
        ),
        now=NOW,
    )
    groups = body["filterGroups"]
    assert len(groups) == 2
    # The shared condition is repeated into both groups, not dropped.
    assert all(g["filters"][0]["propertyName"] == "domain" for g in groups)
    assert groups[0]["filters"][1]["operator"] == "LT"
    assert groups[1]["filters"][1]["operator"] == "NOT_HAS_PROPERTY"


def test_relative_dates_become_epoch_milliseconds() -> None:
    body = compile_hubspot(spec(Condition("enriched_at", "older_than_days", 30)), now=NOW)
    value = body["filterGroups"][0]["filters"][0]["value"]
    assert isinstance(value, int)
    assert value == int((NOW.timestamp() - 30 * 86400) * 1000)


def test_in_uses_the_plural_values_key() -> None:
    body = compile_hubspot(spec(Condition("lifecycle", "in", ["customer", "evangelist"])))
    assert body["filterGroups"][0]["filters"][0]["values"] == ["customer", "evangelist"]


def test_page_size_is_capped_at_the_api_maximum() -> None:
    assert compile_hubspot(spec(limit=5000))["limit"] == 100


def test_runaway_or_unknown_expansion_is_refused() -> None:
    """Each or_unknown doubles the group count; five of them is 32 groups."""
    conditions = [
        Condition("website", "equals", str(i), or_unknown=True) for i in range(5)
    ]
    with pytest.raises(FilterError, match=f"over the {MAX_FILTER_GROUPS} cap"):
        compile_hubspot(spec(*conditions))


def test_raw_hubspot_json_is_used_verbatim() -> None:
    s = FilterSpec(
        name="t",
        conditions=[Condition("website", "is_known")],  # ignored
        raw={"hubspot": {"filterGroups": [{"filters": [{"propertyName": "custom"}]}]}},
    )
    body = compile_hubspot(s)
    assert body["filterGroups"][0]["filters"][0]["propertyName"] == "custom"


# --------------------------------------------------------------------------- #
# Salesforce
# --------------------------------------------------------------------------- #


def test_soql_where_is_anded() -> None:
    where = compile_soql_where(
        spec(Condition("website", "is_known"), Condition("lifecycle", "not_equals", "Customer"))
    )
    assert where == "Website != null AND Type != 'Customer'"


def test_soql_uses_parentheses_instead_of_expansion() -> None:
    """SOQL can express 'A OR B' directly, so no cross product is needed."""
    where = compile_soql_where(
        spec(Condition("enriched_at", "older_than_days", 90, or_unknown=True)), now=NOW
    )
    assert where == (
        "(GTM_Enriched_At__c < 2026-06-12T12:00:00Z OR GTM_Enriched_At__c = null)"
    )


def test_soql_datetime_literals_are_unquoted() -> None:
    where = compile_soql_where(spec(Condition("enriched_at", "newer_than_days", 1)), now=NOW)
    assert "'" not in where
    assert "2026-09-09T12:00:00Z" in where


def test_soql_escapes_quotes_in_values() -> None:
    where = compile_soql_where(spec(Condition("lifecycle", "equals", "O'Brien")))
    assert where == "Type = 'O\\'Brien'"


def test_soql_rejects_an_unsafe_field_name() -> None:
    s = FilterSpec(
        name="t",
        fields={"x": FieldAlias(salesforce="Name FROM Account WHERE Id != null--")},
        conditions=[Condition("x", "is_known")],
    )
    with pytest.raises(FilterError, match="Unsafe Salesforce field name"):
        compile_soql_where(s)


def test_full_soql_query_includes_select_and_limit() -> None:
    query = compile_soql(
        spec(Condition("website", "is_known"), limit=50), "Account", ["Id", "Website"]
    )
    assert query == "SELECT Id, Website FROM Account WHERE Website != null LIMIT 50"


def test_raw_soql_where_is_used_verbatim() -> None:
    s = FilterSpec(
        name="t",
        conditions=[Condition("website", "is_known")],  # ignored
        raw={"salesforce": {"where": "AnnualRevenue > 1000000"}},
    )
    assert compile_soql_where(s) == "AnnualRevenue > 1000000"


def test_the_same_spec_compiles_for_both_systems() -> None:
    """The whole point: write it once."""
    s = spec(
        Condition("website", "is_known"),
        Condition("lifecycle", "not_equals", "customer"),
    )
    assert "domain" in str(compile_hubspot(s))
    assert "Website" in compile_soql_where(s)
