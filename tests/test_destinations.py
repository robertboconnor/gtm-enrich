"""Destinations: the shared upsert cycle, and each adapter against a mock transport."""

from __future__ import annotations

import json

import httpx
import pytest

from gtm_enrich.destinations.base import DestinationError
from gtm_enrich.destinations.dryrun import DryRunDestination
from gtm_enrich.destinations.hubspot import HubSpotDestination
from gtm_enrich.destinations.salesforce import SalesforceDestination
from gtm_enrich.mapping import build_record


def mock_client(handler, base_url: str = "https://api.example") -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url=base_url)


# --------------------------------------------------------------------------- #
# dry run
# --------------------------------------------------------------------------- #


def test_dryrun_writes_json_and_csv(enrichment, mapping, tmp_path) -> None:
    dest = DryRunDestination(output_dir=tmp_path, shape="hubspot")
    record = build_record(enrichment, mapping, "hubspot")

    result = dest.upsert(enrichment, record)
    assert result.action == "would_create"
    assert "gtm_icp_fit_score" in result.changed_fields

    paths = dest.flush()
    payloads = json.loads(paths["json"].read_text())
    assert payloads[0]["properties"]["gtm_icp_fit_score"] == 78
    assert "domain,name" in paths["csv"].read_text().splitlines()[0]


def test_dryrun_flush_is_idempotent(enrichment, mapping, tmp_path) -> None:
    dest = DryRunDestination(output_dir=tmp_path, shape="hubspot")
    dest.upsert(enrichment, build_record(enrichment, mapping, "hubspot"))
    dest.flush()
    dest.close()  # must not write a second, empty pair of files
    assert len(list(tmp_path.glob("payloads-*.json"))) == 1


def test_dryrun_can_preview_salesforce_shape(enrichment, mapping, tmp_path) -> None:
    dest = DryRunDestination(output_dir=tmp_path, shape="salesforce")
    dest.upsert(enrichment, build_record(enrichment, mapping, "salesforce"))
    payloads = json.loads(dest.flush()["json"].read_text())
    assert payloads[0]["object_type"] == "Account"
    assert "GTM_ICP_Fit_Score__c" in payloads[0]["properties"]


# --------------------------------------------------------------------------- #
# HubSpot
# --------------------------------------------------------------------------- #


def test_hubspot_creates_when_no_match(enrichment, mapping) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(201, json={"id": "999"})

    dest = HubSpotDestination(token="t", client=mock_client(handler))
    result = dest.upsert(enrichment, build_record(enrichment, mapping, "hubspot"))

    assert result.action == "created"
    assert result.record_id == "999"
    assert seen == ["POST /crm/v3/objects/companies/search", "POST /crm/v3/objects/companies"]


def test_hubspot_updates_only_changed_fields(enrichment, mapping) -> None:
    record = build_record(enrichment, mapping, "hubspot")
    existing = dict(record.properties)
    existing["gtm_icp_fit_score"] = "12"  # stale score, everything else current
    patched: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": [{"id": "42", "properties": existing}]})
        patched.update(json.loads(request.content)["properties"])
        return httpx.Response(200, json={"id": "42"})

    dest = HubSpotDestination(token="t", client=mock_client(handler))
    result = dest.upsert(enrichment, record)

    assert result.action == "updated"
    assert result.record_id == "42"
    assert list(patched) == ["gtm_icp_fit_score"]
    assert patched["gtm_icp_fit_score"] == 78


def test_hubspot_skips_when_nothing_changed(enrichment, mapping) -> None:
    record = build_record(enrichment, mapping, "hubspot")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200, json={"results": [{"id": "7", "properties": dict(record.properties)}]}
            )
        raise AssertionError("must not write when nothing changed")

    dest = HubSpotDestination(token="t", client=mock_client(handler))
    assert dest.upsert(enrichment, record).action == "skipped"


def test_hubspot_honours_retry_after_then_succeeds(enrichment, mapping, monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("gtm_enrich.destinations.hubspot.time.sleep", slept.append)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "3"}, json={})
            return httpx.Response(200, json={"results": []})
        return httpx.Response(201, json={"id": "1"})

    dest = HubSpotDestination(token="t", client=mock_client(handler))
    assert dest.upsert(enrichment, build_record(enrichment, mapping, "hubspot")).action == "created"
    assert slept == [3.0]


def test_hubspot_api_error_becomes_a_failed_write_not_an_exception(enrichment, mapping) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Property gtm_icp_fit_score does not exist")

    dest = HubSpotDestination(token="t", client=mock_client(handler))
    result = dest.upsert(enrichment, build_record(enrichment, mapping, "hubspot"))
    assert result.action == "failed"
    assert "does not exist" in result.error


def test_hubspot_requires_a_token(monkeypatch) -> None:
    monkeypatch.delenv("HUBSPOT_PRIVATE_APP_TOKEN", raising=False)
    with pytest.raises(DestinationError, match="HUBSPOT_PRIVATE_APP_TOKEN"):
        HubSpotDestination()


# --------------------------------------------------------------------------- #
# Salesforce
# --------------------------------------------------------------------------- #


def test_salesforce_creates_when_query_is_empty(enrichment, mapping) -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/query"):
            queries.append(request.url.params["q"])
            return httpx.Response(200, json={"records": []})
        return httpx.Response(201, json={"id": "001XYZ"})

    dest = SalesforceDestination(
        client=mock_client(handler),
        field_names=["GTM_ICP_Fit_Score__c"],
    )
    result = dest.upsert(enrichment, build_record(enrichment, mapping, "salesforce"))

    assert result.action == "created"
    assert result.record_id == "001XYZ"
    # Matches on the bare domain so http/https and www variants all hit.
    assert "Website LIKE '%acme.example%'" in queries[0]
    assert queries[0].startswith("SELECT GTM_ICP_Fit_Score__c, Id, Website FROM Account")


def test_salesforce_updates_and_strips_attributes(enrichment, mapping) -> None:
    record = build_record(enrichment, mapping, "salesforce")
    patched: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/query"):
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "attributes": {"type": "Account"},
                            "Id": "001ABC",
                            "GTM_ICP_Fit_Score__c": 12,
                        }
                    ]
                },
            )
        patched.update(json.loads(request.content))
        return httpx.Response(204)

    dest = SalesforceDestination(client=mock_client(handler), field_names=["GTM_ICP_Fit_Score__c"])
    result = dest.upsert(enrichment, record)

    assert result.action == "updated"
    assert patched["GTM_ICP_Fit_Score__c"] == 78
    assert "attributes" not in patched


def test_salesforce_rejects_injection_in_config() -> None:
    with pytest.raises(DestinationError, match="Unsafe Salesforce"):
        SalesforceDestination(object_type="Account WHERE Id != null--")


def test_salesforce_rejects_unsafe_match_key(enrichment, mapping) -> None:
    record = build_record(enrichment, mapping, "salesforce")
    record.match_key = "Website'--"

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not issue a query with an unsafe field name")

    dest = SalesforceDestination(client=mock_client(handler))
    assert dest.upsert(enrichment, record).action == "failed"


def test_salesforce_needs_credentials(monkeypatch) -> None:
    for var in ("SF_INSTANCE_URL", "SF_ACCESS_TOKEN", "SF_CLIENT_ID", "SF_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(DestinationError, match="Salesforce credentials not found"):
        SalesforceDestination()
