"""Salesforce as a source: SOQL, paged.

Salesforce returns 2,000 records per page and a `nextRecordsUrl` to continue.
`LIMIT` in the query caps the total, so paging and the limit interact -- this
asks for what it needs and stops.

Auth is shared with the destination: either a session token or the OAuth client
credentials flow against a connected app.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from ..destinations.salesforce import API_VERSION, SalesforceDestination
from ..filters import FilterSpec, compile_soql
from .base import Source, SourceError, SourceRecord

log = logging.getLogger(__name__)


class SalesforceSource(Source):
    name = "salesforce"

    def __init__(
        self,
        *,
        object_type: str = "Account",
        domain_field: str = "Website",
        extra_fields: list[str] | None = None,
        client: httpx.Client | None = None,
        instance_url: str | None = None,
        access_token: str | None = None,
    ) -> None:
        self.object_type = object_type
        self.domain_field = domain_field
        self.extra_fields = extra_fields or []
        if client is not None:
            self._client = client
        else:
            # Same credential resolution as the destination; no second mechanism.
            instance, token = SalesforceDestination._authenticate(instance_url, access_token)
            self._client = httpx.Client(
                base_url=instance,
                timeout=30.0,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(4):
            resp = self._client.request(method, path, **kwargs)
            if resp.status_code in (429, 503) and attempt < 3:
                time.sleep(float(resp.headers.get("Retry-After", 2**attempt)))
                continue
            if resp.status_code >= 500 and attempt < 3:
                time.sleep(2**attempt)
                continue
            if resp.status_code >= 400:
                raise SourceError(
                    f"Salesforce {method} {path} -> {resp.status_code}: {resp.text[:300]}"
                )
            return resp
        raise SourceError(f"Salesforce {method} {path}: exhausted retries.")

    def _fields(self) -> list[str]:
        return list(dict.fromkeys(["Id", self.domain_field, *self.extra_fields]))

    def describe(self, spec: FilterSpec) -> str:
        return compile_soql(spec, self.object_type, self._fields())

    def fetch(self, spec: FilterSpec, limit: int | None = None) -> list[SourceRecord]:
        target = limit or spec.limit
        query = compile_soql(spec, self.object_type, self._fields())

        records: list[SourceRecord] = []
        resp = self._request(
            "GET", f"/services/data/{API_VERSION}/query", params={"q": query}
        )

        while True:
            payload = resp.json()
            for row in payload.get("records", []):
                row = {k: v for k, v in row.items() if k != "attributes"}
                domain = row.get(self.domain_field)
                if not domain:
                    continue
                records.append(
                    SourceRecord(
                        domain=str(domain),
                        record_id=str(row.get("Id")) if row.get("Id") else None,
                        properties=row,
                        source=self.name,
                    )
                )
                if len(records) >= target:
                    return records

            next_url = payload.get("nextRecordsUrl")
            if payload.get("done", True) or not next_url:
                return records
            resp = self._request("GET", next_url)

    def close(self) -> None:
        self._client.close()
