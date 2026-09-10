"""The webhook service: verify, deduplicate, enqueue, return.

Every test here is about something that bites in production -- an unsigned
request, a replayed one, a duplicate delivery, or a record that arrives before
its website does.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gtm_enrich.server.app import create_app
from gtm_enrich.server.queue import JobQueue
from gtm_enrich.server.security import (
    MAX_SIGNATURE_AGE_MS,
    SignatureError,
    hubspot_signature,
    verify_hubspot,
    verify_shared_secret,
)
from gtm_enrich.state import StateStore

SECRET = "client-secret"
URL = "http://testserver/webhooks/hubspot"


def now_ms() -> int:
    return int(time.time() * 1000)


def signed(body: str, secret: str = SECRET, timestamp: str | None = None):
    ts = timestamp or str(now_ms())
    return {
        "X-HubSpot-Signature-v3": hubspot_signature(secret, "POST", URL, body, ts),
        "X-HubSpot-Request-Timestamp": ts,
    }


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HUBSPOT_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("GTM_WEBHOOK_SECRET", "shared-secret")
    monkeypatch.chdir(tmp_path)  # queue + state land in a temp .cache
    Path("config").mkdir()
    repo = Path(__file__).resolve().parents[1] / "config"
    for name in ("icp.yaml", "mapping.yaml"):
        Path("config", name).write_text((repo / name).read_text())
    app = create_app(start_worker=False)
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- #
# signatures
# --------------------------------------------------------------------------- #


def test_a_correct_signature_verifies() -> None:
    ts = str(now_ms())
    body = '[{"objectId":1}]'
    verify_hubspot(
        secret=SECRET, method="POST", uri=URL, body=body,
        signature=hubspot_signature(SECRET, "POST", URL, body, ts), timestamp=ts,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("body", "tampered"), ("uri", "http://evil/x"), ("method", "GET")],
)
def test_changing_any_signed_component_fails(field: str, value: str) -> None:
    ts = str(now_ms())
    parts = {"method": "POST", "uri": URL, "body": '[{"objectId":1}]'}
    signature = hubspot_signature(SECRET, parts["method"], parts["uri"], parts["body"], ts)
    parts[field] = value
    with pytest.raises(SignatureError, match="does not match"):
        verify_hubspot(secret=SECRET, **parts, signature=signature, timestamp=ts)


def test_an_old_request_is_rejected_as_a_replay() -> None:
    old = str(now_ms() - MAX_SIGNATURE_AGE_MS - 1000)
    body = "[]"
    with pytest.raises(SignatureError, match="replay guard"):
        verify_hubspot(
            secret=SECRET, method="POST", uri=URL, body=body,
            signature=hubspot_signature(SECRET, "POST", URL, body, old), timestamp=old,
        )


def test_a_missing_secret_refuses_rather_than_waves_through() -> None:
    with pytest.raises(SignatureError, match="Refusing to process"):
        verify_hubspot(
            secret=None, method="POST", uri=URL, body="[]", signature="x", timestamp="1"
        )


def test_shared_secret_must_match(monkeypatch) -> None:
    monkeypatch.setenv("GTM_WEBHOOK_SECRET", "right")
    verify_shared_secret("right")
    with pytest.raises(SignatureError, match="missing or incorrect"):
        verify_shared_secret("wrong")


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #


def test_health_reports_configuration(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["destination"] == "dryrun"  # never a live CRM by default
    assert "queue" in body


def test_an_unsigned_webhook_is_rejected(client: TestClient) -> None:
    response = client.post("/webhooks/hubspot", content="[]")
    assert response.status_code == 401


def test_a_signed_property_change_is_queued(client: TestClient) -> None:
    body = json.dumps(
        [{
            "eventId": 1, "objectId": 42, "subscriptionType": "company.propertyChange",
            "propertyName": "domain", "propertyValue": "acme.com",
        }]
    )
    response = client.post("/webhooks/hubspot", content=body, headers=signed(body))
    assert response.status_code == 200
    assert response.json() == {"received": 1, "queued": 1, "skipped": 0}


def test_a_redelivered_event_is_not_queued_twice(client: TestClient) -> None:
    """Webhooks retry. Without dedupe you pay for the same enrichment twice."""
    body = json.dumps(
        [{
            "eventId": 7, "objectId": 42, "subscriptionType": "company.propertyChange",
            "propertyName": "domain", "propertyValue": "acme.com",
        }]
    )
    first = client.post("/webhooks/hubspot", content=body, headers=signed(body))
    second = client.post("/webhooks/hubspot", content=body, headers=signed(body))
    assert first.json()["queued"] == 1
    assert second.json()["queued"] == 0
    assert second.json()["skipped"] == 1


def test_irrelevant_property_changes_are_ignored(client: TestClient) -> None:
    """Re-enriching because someone edited a phone number is pure waste."""
    body = json.dumps(
        [{
            "eventId": 2, "objectId": 42, "subscriptionType": "company.propertyChange",
            "propertyName": "phone", "propertyValue": "555-0100",
        }]
    )
    response = client.post("/webhooks/hubspot", content=body, headers=signed(body))
    assert response.json()["queued"] == 0


def test_a_creation_event_is_queued_without_a_domain(client: TestClient) -> None:
    """Creation carries an object id and nothing else; the worker resolves it."""
    body = json.dumps([{"eventId": 3, "objectId": 99, "subscriptionType": "company.creation"}])
    response = client.post("/webhooks/hubspot", content=body, headers=signed(body))
    assert response.json()["queued"] == 1


def test_salesforce_endpoint_needs_the_shared_secret(client: TestClient) -> None:
    payload = {"Id": "001", "Website": "https://acme.com"}
    assert client.post("/webhooks/salesforce", json=payload).status_code == 401
    ok = client.post(
        "/webhooks/salesforce", json=payload, headers={"X-GTM-Secret": "shared-secret"}
    )
    assert ok.status_code == 200
    assert ok.json()["queued"] == 1


def test_malformed_json_is_a_400_not_a_500(client: TestClient) -> None:
    body = "{not json"
    response = client.post("/webhooks/hubspot", content=body, headers=signed(body))
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# queue
# --------------------------------------------------------------------------- #


def test_claim_is_atomic_so_two_workers_cannot_take_one_job(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "q.db")
    queue.enqueue({"domain": "acme.com"})
    assert queue.claim() is not None
    assert queue.claim() is None


def test_a_delayed_job_is_not_claimable_yet(tmp_path: Path) -> None:
    """Used when a record arrives before its website does."""
    queue = JobQueue(tmp_path / "q.db")
    queue.enqueue({"domain": "acme.com"}, delay_seconds=300)
    assert queue.claim() is None


def test_failures_retry_then_dead_letter(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "q.db")
    queue.enqueue({"domain": "acme.com"})
    for _ in range(3):
        job = queue.claim()
        assert job is not None
        queue.fail(job.id, "boom", retry_in_seconds=0)
    # Kept, not deleted: a failed job you cannot read is one you cannot debug.
    assert queue.stats().get("failed") == 1
    assert queue.claim() is None


def test_state_dedupe_is_first_write_wins(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "s.db")
    assert store.mark_seen("evt-1") is True
    assert store.mark_seen("evt-1") is False


def test_purging_old_events_leaves_recent_ones(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "s.db")
    store.mark_seen("evt-1")
    assert store.purge_seen(older_than_hours=72) == 0
    assert store.mark_seen("evt-1") is False  # still remembered
