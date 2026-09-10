"""Sources: paging, preflight, and turning two very different APIs into one list."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from gtm_enrich.filters import Condition, FieldAlias, FilterSpec
from gtm_enrich.sources import (
    SOURCE_REQUIREMENTS,
    SOURCES,
    CsvSource,
    HubSpotSource,
    SalesforceSource,
    SourceError,
    SourceNotConfigured,
    build_source,
)

SPEC = FilterSpec(
    name="t",
    fields={"website": FieldAlias(hubspot="domain", salesforce="Website")},
    conditions=[Condition("website", "is_known")],
    limit=250,
)


def mock_client(handler, base_url="https://api.example") -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url=base_url)


def test_every_source_is_registered_with_a_requirement() -> None:
    assert set(SOURCES) == set(SOURCE_REQUIREMENTS)


def test_unknown_source_is_a_clean_error() -> None:
    with pytest.raises(SourceError, match="Unknown source"):
        build_source("pipedrive")


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #


def test_csv_source_reads_a_domain_column(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    path.write_text("company,domain\nAcme,acme.com\nBeta,beta.io\n")
    records = CsvSource(path).fetch(SPEC)
    assert [r.domain for r in records] == ["acme.com", "beta.io"]
    assert all(r.source == "csv" for r in records)


def test_csv_source_respects_a_limit(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("acme.com\nbeta.io\ngamma.dev\n")
    assert len(CsvSource(path).fetch(SPEC, limit=2)) == 2


def test_csv_source_says_filters_do_not_apply(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    path.write_text("domain\nacme.com\n")
    assert "filters do not apply" in CsvSource(path).describe(SPEC)


def test_missing_file_is_a_clean_error(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="No such file"):
        CsvSource(tmp_path / "nope.csv")


# --------------------------------------------------------------------------- #
# HubSpot
# --------------------------------------------------------------------------- #


def hubspot_page(results, after=None):
    body = {"total": 999, "results": results}
    if after:
        body["paging"] = {"next": {"after": after}}
    return body


def company(idx: int):
    return {"id": str(idx), "properties": {"domain": f"acme{idx}.com", "name": f"Acme {idx}"}}


def test_hubspot_source_pages_until_it_has_enough() -> None:
    seen_afters: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_afters.append(body.get("after"))
        page = len(seen_afters)
        results = [company(i) for i in range((page - 1) * 2, page * 2)]
        return httpx.Response(200, json=hubspot_page(results, after=str(page * 2)))

    source = HubSpotSource(token="t", client=mock_client(handler))
    records = source.fetch(SPEC, limit=5)

    assert len(records) == 5
    assert seen_afters == [None, "2", "4"]  # cursor threaded through
    assert records[0].record_id == "0"


def test_hubspot_source_stops_when_paging_runs_out() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=hubspot_page([company(1)]))  # no paging key

    records = HubSpotSource(token="t", client=mock_client(handler)).fetch(SPEC, limit=100)
    assert len(records) == 1


def test_hubspot_source_skips_records_with_no_domain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=hubspot_page(
                [{"id": "1", "properties": {"domain": None, "name": "No site"}}, company(2)]
            ),
        )

    records = HubSpotSource(token="t", client=mock_client(handler)).fetch(SPEC, limit=10)
    assert [r.domain for r in records] == ["acme2.com"]


def test_hubspot_preflight_names_the_missing_property() -> None:
    """HubSpot answers 'There was a problem with the request' and nothing else.
    Verified against a live portal, which is why this check exists."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "properties" in request.url.path:
            return httpx.Response(200, json={"results": [{"name": "domain"}]})
        raise AssertionError("preflight must not run the search")

    spec = FilterSpec(
        name="t",
        fields={"enriched": FieldAlias(hubspot="gtm_enriched_at")},
        conditions=[Condition("enriched", "is_known")],
    )
    problems = HubSpotSource(token="t", client=mock_client(handler)).preflight(spec)
    assert len(problems) == 1
    assert "gtm_enriched_at" in problems[0]
    assert "gtm-enrich fields" in problems[0]


def test_hubspot_preflight_passes_when_properties_exist() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"name": "domain"}, {"name": "name"}]})

    assert HubSpotSource(token="t", client=mock_client(handler)).preflight(SPEC) == []


def test_hubspot_preflight_checks_raw_queries_too() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"name": "domain"}]})

    spec = FilterSpec(
        name="t",
        raw={"hubspot": {"filterGroups": [{"filters": [{"propertyName": "made_up"}]}]}},
    )
    problems = HubSpotSource(token="t", client=mock_client(handler)).preflight(spec)
    assert "made_up" in problems[0]


def test_hubspot_get_domain_reads_one_record() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/companies/42")
        return httpx.Response(200, json={"id": "42", "properties": {"domain": "acme.com"}})

    assert HubSpotSource(token="t", client=mock_client(handler)).get_domain("42") == "acme.com"


def test_hubspot_get_domain_returns_none_when_empty() -> None:
    """A company created a second ago usually has no website yet."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "42", "properties": {"domain": ""}})

    assert HubSpotSource(token="t", client=mock_client(handler)).get_domain("42") is None


def test_hubspot_source_needs_a_token(monkeypatch) -> None:
    monkeypatch.delenv("HUBSPOT_PRIVATE_APP_TOKEN", raising=False)
    with pytest.raises(SourceNotConfigured, match="HUBSPOT_PRIVATE_APP_TOKEN"):
        HubSpotSource()


# --------------------------------------------------------------------------- #
# Salesforce
# --------------------------------------------------------------------------- #


def test_salesforce_source_follows_next_records_url() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "nextRecords" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "done": True,
                    "records": [
                        {"attributes": {}, "Id": "002", "Website": "https://beta.io"}
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "done": False,
                "nextRecordsUrl": "/services/data/v61.0/query/nextRecords-2000",
                "records": [{"attributes": {}, "Id": "001", "Website": "https://acme.com"}],
            },
        )

    source = SalesforceSource(client=mock_client(handler))
    records = source.fetch(SPEC, limit=10)

    assert [r.domain for r in records] == ["https://acme.com", "https://beta.io"]
    assert len(calls) == 2
    assert "attributes" not in records[0].properties


def test_salesforce_source_stops_at_the_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "done": False,
                "nextRecordsUrl": "/next",
                "records": [
                    {"attributes": {}, "Id": str(i), "Website": f"https://a{i}.com"}
                    for i in range(10)
                ],
            },
        )

    records = SalesforceSource(client=mock_client(handler)).fetch(SPEC, limit=3)
    assert len(records) == 3


def test_salesforce_describe_shows_the_query() -> None:
    query = SalesforceSource(client=mock_client(lambda r: httpx.Response(200))).describe(SPEC)
    assert query.startswith("SELECT Id, Website FROM Account WHERE Website != null")


def test_salesforce_api_errors_surface() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="MALFORMED_QUERY: unexpected token")

    with pytest.raises(SourceError, match="MALFORMED_QUERY"):
        SalesforceSource(client=mock_client(handler)).fetch(SPEC)
