"""HubSpot company records via the CRM v3 API.

Auth is a private app token (`HUBSPOT_PRIVATE_APP_TOKEN`) -- the modern
replacement for API keys, scoped per app. The token needs
`crm.objects.companies.read` and `crm.objects.companies.write`.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from ..models import CrmRecord
from .base import Destination, DestinationError

log = logging.getLogger(__name__)

BASE_URL = "https://api.hubapi.com"
TOKEN_ENV = "HUBSPOT_PRIVATE_APP_TOKEN"


class HubSpotDestination(Destination):
    name = "hubspot"

    def __init__(
        self,
        token: str | None = None,
        *,
        object_type: str = "companies",
        field_names: list[str] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        token = token or os.getenv(TOKEN_ENV)
        if not token:
            raise DestinationError(
                f"{TOKEN_ENV} is not set. Create a HubSpot private app with company "
                "read/write scopes and export its token, or run with --dest dryrun."
            )
        self.object_type = object_type
        self.field_names = field_names or []
        self._client = client or httpx.Client(
            base_url=BASE_URL,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    # -- HTTP ------------------------------------------------------------- #

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One request, retrying on HubSpot's 429 and on 5xx.

        HubSpot returns a `Retry-After` header on rate limits; honour it rather
        than guessing, or the retry just burns another unit of quota.
        """
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
                raise DestinationError(
                    f"HubSpot {method} {path} -> {resp.status_code}: {resp.text[:400]}"
                )
            return resp
        raise DestinationError(f"HubSpot {method} {path}: still rate limited after 4 attempts.")

    # -- Destination ------------------------------------------------------ #

    def find(self, record: CrmRecord) -> tuple[str, dict[str, Any]] | None:
        body = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": record.match_key,
                            "operator": "EQ",
                            "value": record.match_value,
                        }
                    ]
                }
            ],
            # Ask for the fields we intend to write, so the diff is against
            # real current values rather than an empty dict.
            "properties": sorted({*self.field_names, *record.properties, record.match_key}),
            "limit": 1,
        }
        resp = self._request("POST", f"/crm/v3/objects/{self.object_type}/search", json=body)
        results = resp.json().get("results", [])
        if not results:
            return None
        hit = results[0]
        return str(hit["id"]), dict(hit.get("properties") or {})

    def create(self, record: CrmRecord) -> str:
        resp = self._request(
            "POST",
            f"/crm/v3/objects/{self.object_type}",
            json={"properties": record.properties},
        )
        return str(resp.json()["id"])

    def update(self, record_id: str, properties: dict[str, Any]) -> None:
        self._request(
            "PATCH",
            f"/crm/v3/objects/{self.object_type}/{record_id}",
            json={"properties": properties},
        )

    def close(self) -> None:
        self._client.close()
