"""HubSpot as a source: the CRM search API, paged.

Two things here are worth more than the plumbing.

**Preflight.** Referencing a property that does not exist in the portal gets you
`{"status":"error","message":"There was a problem with the request."}` and no
indication of which property. Verified against a live portal. So before running
anything, this reads the portal's property list and names the missing fields
itself, pointing at the command that tells you what to create.

**Paging.** Search returns a cursor in `paging.next.after`; 100 records is the
per-page maximum whatever you ask for.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from ..filters import FilterSpec, compile_hubspot
from .base import Source, SourceError, SourceNotConfigured, SourceRecord

log = logging.getLogger(__name__)

BASE_URL = "https://api.hubapi.com"
TOKEN_ENV = "HUBSPOT_PRIVATE_APP_TOKEN"
PAGE_SIZE = 100


class HubSpotSource(Source):
    name = "hubspot"

    def __init__(
        self,
        token: str | None = None,
        *,
        object_type: str = "companies",
        domain_property: str = "domain",
        extra_properties: list[str] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        token = token or os.getenv(TOKEN_ENV)
        if not token and client is None:
            raise SourceNotConfigured(
                f"{TOKEN_ENV} is not set, which the HubSpot source requires. Create a "
                "private app with company read scope and export its token."
            )
        self.object_type = object_type
        self.domain_property = domain_property
        self.extra_properties = extra_properties or []
        self._client = client or httpx.Client(
            base_url=BASE_URL,
            timeout=30.0,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    # -- HTTP ------------------------------------------------------------- #

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(4):
            resp = self._client.request(method, path, **kwargs)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 2**attempt))
                log.info("HubSpot rate limited, sleeping %.1fs", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < 3:
                time.sleep(2**attempt)
                continue
            if resp.status_code >= 400:
                raise SourceError(
                    f"HubSpot {method} {path} -> {resp.status_code}: {resp.text[:300]}"
                )
            return resp
        raise SourceError(f"HubSpot {method} {path}: still rate limited after 4 attempts.")

    # -- Source ----------------------------------------------------------- #

    def available_properties(self) -> set[str]:
        resp = self._request("GET", f"/crm/v3/properties/{self.object_type}")
        return {r["name"] for r in resp.json().get("results", [])}

    def _referenced_properties(self, spec: FilterSpec) -> list[str]:
        if "hubspot" in spec.raw:
            groups = spec.raw["hubspot"].get("filterGroups", [])
            return [f["propertyName"] for g in groups for f in g.get("filters", [])]
        return [spec.resolve(c.field, "hubspot") for c in spec.conditions]

    def preflight(self, spec: FilterSpec) -> list[str]:
        """Name the missing properties, because HubSpot's own error will not."""
        try:
            available = self.available_properties()
        except SourceError as exc:
            return [f"could not read the portal's property list: {exc}"]

        wanted = set(self._referenced_properties(spec)) | {self.domain_property}
        missing = sorted(w for w in wanted if w not in available)
        if not missing:
            return []
        return [
            f"these company properties do not exist in this portal: {', '.join(missing)}. "
            "HubSpot rejects the whole query without saying which one. Run "
            "`gtm-enrich fields --dest hubspot` for the list to create, or edit the filter."
        ]

    def describe(self, spec: FilterSpec) -> str:
        import json

        body = compile_hubspot(spec)
        return f"POST /crm/v3/objects/{self.object_type}/search\n{json.dumps(body, indent=2)}"

    def fetch(self, spec: FilterSpec, limit: int | None = None) -> list[SourceRecord]:
        body = compile_hubspot(spec)
        body["properties"] = sorted(
            {self.domain_property, "name", *self.extra_properties}
        )
        body["limit"] = PAGE_SIZE

        target = limit or spec.limit
        records: list[SourceRecord] = []
        after: str | None = None

        while len(records) < target:
            if after:
                body["after"] = after
            resp = self._request(
                "POST", f"/crm/v3/objects/{self.object_type}/search", json=body
            )
            payload = resp.json()

            for hit in payload.get("results", []):
                props = hit.get("properties") or {}
                domain = props.get(self.domain_property)
                if not domain:
                    continue  # matched the filter but has nothing to scrape
                records.append(
                    SourceRecord(
                        domain=domain,
                        record_id=str(hit["id"]),
                        properties=props,
                        source=self.name,
                    )
                )
                if len(records) >= target:
                    break

            after = (payload.get("paging") or {}).get("next", {}).get("after")
            if not after:
                break

        return records

    def get_domain(self, record_id: str) -> str | None:
        """Look up one record's domain.

        Needed by the webhook path: a `company.creation` event carries an object
        id and nothing else, so the domain has to be fetched before enrichment
        can start -- and it is frequently still empty at that moment.
        """
        resp = self._request(
            "GET",
            f"/crm/v3/objects/{self.object_type}/{record_id}",
            params={"properties": self.domain_property},
        )
        return (resp.json().get("properties") or {}).get(self.domain_property) or None

    def close(self) -> None:
        self._client.close()
