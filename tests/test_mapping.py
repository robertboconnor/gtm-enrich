"""Mapping: dotted paths, per-destination coercion, and the no-op diff."""

from __future__ import annotations

import pytest

from gtm_enrich.mapping import (
    MappingError,
    build_record,
    coerce,
    diff_properties,
    mapping_field_names,
    resolve_path,
)
from gtm_enrich.models import EnrichmentResult


def test_resolve_path_walks_models_and_dicts(enrichment) -> None:
    assert resolve_path(enrichment, "analysis.icp_fit_score") == 78
    assert resolve_path(enrichment, "provenance.analyzer") == "llm:claude-opus-5"
    assert resolve_path(enrichment, "page_signals.has_demo_cta") is True
    assert resolve_path(enrichment, "domain") == "acme.example"


def test_resolve_path_rejects_unknown_field(enrichment) -> None:
    with pytest.raises(MappingError, match="no field 'nope'"):
        resolve_path(enrichment, "analysis.nope")


def test_resolve_path_returns_none_through_missing_dict_key(enrichment) -> None:
    assert resolve_path(enrichment, "page_signals.has_unicorns") is None


def test_coerce_list_joins_with_semicolons() -> None:
    assert coerce(["a", "b"], "list", "hubspot") == "a; b"
    assert coerce([], "list", "hubspot") is None


def test_coerce_boolean_differs_by_destination() -> None:
    # HubSpot checkbox properties take strings; Salesforce takes real booleans.
    assert coerce(True, "boolean", "hubspot") == "true"
    assert coerce(True, "boolean", "salesforce") is True
    assert coerce(False, "boolean", "salesforce") is False


def test_coerce_datetime_is_iso8601(enrichment) -> None:
    value = coerce(enrichment.provenance.analyzed_at, "datetime", "hubspot")
    assert value.startswith("2026-01-02T")


def test_coerce_drops_empty_strings() -> None:
    assert coerce("   ", "string", "hubspot") is None
    assert coerce(None, "string", "hubspot") is None


def test_build_record_uses_hubspot_names(enrichment, mapping) -> None:
    record = build_record(enrichment, mapping, "hubspot")
    assert record.object_type == "companies"
    assert record.match_key == "domain"
    assert record.match_value == "acme.example"
    assert record.properties["gtm_icp_fit_score"] == 78
    assert record.properties["name"] == "Acme Analytics"
    assert record.properties["gtm_has_demo_cta"] == "true"
    assert "GTM_ICP_Fit_Score__c" not in record.properties


def test_build_record_uses_salesforce_names_and_url_match(enrichment, mapping) -> None:
    record = build_record(enrichment, mapping, "salesforce")
    assert record.object_type == "Account"
    assert record.match_key == "Website"
    # Salesforce's Website field holds a URL, not a bare domain.
    assert record.match_value == "https://acme.example"
    assert record.properties["GTM_ICP_Fit_Score__c"] == 78
    assert record.properties["GTM_Has_Demo_CTA__c"] is True


def test_build_record_refuses_failed_enrichment(mapping) -> None:
    failed = EnrichmentResult(domain="broken.example", ok=False, error="timeout")
    with pytest.raises(MappingError, match="failed enrichment"):
        build_record(failed, mapping, "hubspot")


def test_build_record_rejects_unknown_destination(enrichment, mapping) -> None:
    with pytest.raises(MappingError, match="No object configured"):
        build_record(enrichment, mapping, "pipedrive")


def test_diff_only_reports_real_changes() -> None:
    existing = {"score": "78", "name": "Acme", "flag": "true"}
    proposed = {"score": 78, "name": "Acme Analytics", "flag": True}
    changes, names = diff_properties(existing, proposed)
    # "78" vs 78 and "true" vs True are round-trip noise, not changes.
    assert names == ["name"]
    assert changes == {"name": "Acme Analytics"}


def test_diff_treats_missing_as_changed() -> None:
    changes, names = diff_properties({}, {"gtm_category": "analytics"})
    assert names == ["gtm_category"]


def test_diff_ignores_whitespace_only_differences() -> None:
    changes, names = diff_properties({"a": " x "}, {"a": "x"})
    assert names == []


def test_mapping_field_names_covers_every_mapped_field(mapping) -> None:
    names = mapping_field_names(mapping, "salesforce")
    assert "GTM_Enriched_At__c" in names
    assert len(names) == len(mapping.for_destination("salesforce"))
