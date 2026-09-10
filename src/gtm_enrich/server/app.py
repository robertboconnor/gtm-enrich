"""The webhook service.

Three endpoints and one rule: **verify, deduplicate, enqueue, return.** No
enrichment happens on the request path, because both HubSpot and Salesforce
expect an answer in seconds and enrichment takes twenty or more.

The order of those four steps is the security design. Verification comes before
anything is written down, so an unauthenticated caller cannot fill the queue.
Deduplication comes before enqueueing, so a provider retrying a delivery it
already made does not pay for the same enrichment twice.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from ..config import Settings, load_env
from ..state import StateStore
from .queue import JobQueue
from .security import SignatureError, verify_hubspot, verify_shared_secret
from .worker import Worker

log = logging.getLogger(__name__)

# HubSpot subscription types we act on. Creation alone is not enough: a company
# is usually created before its website is known, so the change event on the
# website property is what actually carries a scrapable value.
HUBSPOT_EVENTS = {"company.creation", "company.propertyChange"}
WEBSITE_PROPERTIES = {"domain", "website"}


def create_app(
    *,
    icp_path: Path = Path("config/icp.yaml"),
    mapping_path: Path = Path("config/mapping.yaml"),
    destination: str | None = None,
    start_worker: bool = True,
) -> FastAPI:
    load_env()
    settings = Settings.from_env()
    queue = JobQueue()
    state = StateStore()
    worker = Worker(
        queue=queue,
        settings=settings,
        icp_path=icp_path,
        mapping_path=mapping_path,
        # Dry run unless a real destination is named. A service that writes to a
        # production CRM the moment it boots is not a good default.
        destination=destination or os.getenv("GTM_DESTINATION", "dryrun"),
    )

    app = FastAPI(
        title="gtm-enrich",
        description="Enriches company records in response to CRM webhooks.",
        version="0.1.0",
    )
    app.state.queue = queue
    app.state.store = state
    app.state.worker = worker

    @app.on_event("startup")
    def _startup() -> None:
        state.purge_seen()
        if start_worker:
            worker.start()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        worker.stop()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "destination": worker.destination_name,
            "provider": settings.analyze.provider,
            "scraper": settings.scrape.backend,
            "queue": queue.stats(),
        }

    @app.post("/webhooks/hubspot")
    async def hubspot_webhook(
        request: Request,
        x_hubspot_signature_v3: str | None = Header(default=None),
        x_hubspot_request_timestamp: str | None = Header(default=None),
    ) -> dict[str, Any]:
        body = (await request.body()).decode("utf-8")
        try:
            verify_hubspot(
                secret=os.getenv("HUBSPOT_CLIENT_SECRET"),
                method="POST",
                uri=str(request.url),
                body=body,
                signature=x_hubspot_signature_v3,
                timestamp=x_hubspot_request_timestamp,
            )
        except SignatureError as exc:
            log.warning("rejected a HubSpot webhook: %s", exc)
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        import json

        try:
            events = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Body is not valid JSON.") from exc
        if not isinstance(events, list):
            events = [events]

        queued, skipped = 0, 0
        for event in events:
            if not _hubspot_event_is_interesting(event):
                skipped += 1
                continue
            key = _hubspot_event_key(event)
            if not state.mark_seen(key):
                skipped += 1  # a redelivery of something already handled
                continue
            queue.enqueue(
                {
                    "system": "hubspot",
                    "record_id": str(event.get("objectId")),
                    "domain": _hubspot_event_domain(event),
                    "event": event.get("subscriptionType"),
                }
            )
            queued += 1

        return {"received": len(events), "queued": queued, "skipped": skipped}

    @app.post("/webhooks/salesforce")
    async def salesforce_webhook(
        request: Request,
        x_gtm_secret: str | None = Header(default=None),
    ) -> dict[str, Any]:
        try:
            verify_shared_secret(x_gtm_secret)
        except SignatureError as exc:
            log.warning("rejected a Salesforce webhook: %s", exc)
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        payload = await request.json()
        records = payload if isinstance(payload, list) else [payload]

        queued, skipped = 0, 0
        for record in records:
            record_id = record.get("Id") or record.get("recordId")
            domain = record.get("Website") or record.get("website")
            if not record_id and not domain:
                skipped += 1
                continue
            if not state.mark_seen(f"salesforce:{record_id or domain}"):
                skipped += 1
                continue
            queue.enqueue(
                {"system": "salesforce", "record_id": record_id, "domain": domain}
            )
            queued += 1

        return {"received": len(records), "queued": queued, "skipped": skipped}

    return app


# --------------------------------------------------------------------------- #
# HubSpot event helpers
# --------------------------------------------------------------------------- #


def _hubspot_event_is_interesting(event: dict[str, Any]) -> bool:
    subscription = event.get("subscriptionType")
    if subscription not in HUBSPOT_EVENTS:
        return False
    if subscription == "company.propertyChange":
        # Only the website landing is worth acting on; every other property
        # change would re-enrich a page that has not moved.
        return event.get("propertyName") in WEBSITE_PROPERTIES
    return True


def _hubspot_event_domain(event: dict[str, Any]) -> str | None:
    if event.get("propertyName") in WEBSITE_PROPERTIES:
        return event.get("propertyValue") or None
    return None


def _hubspot_event_key(event: dict[str, Any]) -> str:
    """Stable identity for an event, so a redelivery is recognised.

    HubSpot sends an eventId; falling back to the object plus property plus
    timestamp keeps dedupe working if it is ever absent.
    """
    if event.get("eventId"):
        return f"hubspot:{event['eventId']}"
    return (
        f"hubspot:{event.get('objectId')}:{event.get('propertyName')}:"
        f"{event.get('occurredAt')}"
    )
