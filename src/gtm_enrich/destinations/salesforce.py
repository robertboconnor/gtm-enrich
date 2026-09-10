"""Salesforce Account records via the REST API.

Two ways in, both env-driven:

* `SF_INSTANCE_URL` + `SF_ACCESS_TOKEN` -- paste a session token, fastest for a
  one-off test.
* `SF_CLIENT_ID` + `SF_CLIENT_SECRET` (+ `SF_LOGIN_URL`) -- OAuth 2.0 client
  credentials against a connected app, which is what you would actually run.

Matching is the interesting part. Salesforce has no natural unique key for a
company, so this matches on a configurable field (`Website` by default) and
falls back to creating. See docs/crm-setup.md for why an external-ID field is
the better production answer.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import httpx

from ..models import CrmRecord
from .base import Destination, DestinationError

log = logging.getLogger(__name__)

API_VERSION = "v61.0"
DEFAULT_LOGIN_URL = "https://login.salesforce.com"

# Field and object names are interpolated into SOQL, so they are allow-listed
# rather than escaped -- a name that is not a bare identifier is a config bug.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _check_identifier(value: str, kind: str) -> str:
    if not _IDENTIFIER_RE.match(value):
        raise DestinationError(f"Unsafe Salesforce {kind} name in config: {value!r}")
    return value


def _soql_literal(value: str) -> str:
    """Escape a value for a SOQL string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class SalesforceDestination(Destination):
    name = "salesforce"

    def __init__(
        self,
        *,
        object_type: str = "Account",
        field_names: list[str] | None = None,
        client: httpx.Client | None = None,
        instance_url: str | None = None,
        access_token: str | None = None,
    ) -> None:
        self.object_type = _check_identifier(object_type, "object")
        self.field_names = [_check_identifier(f, "field") for f in (field_names or [])]

        if client is not None:
            self._client = client
            self.instance_url = instance_url or str(client.base_url)
        else:
            self.instance_url, token = self._authenticate(instance_url, access_token)
            self._client = httpx.Client(
                base_url=self.instance_url,
                timeout=30.0,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )

    # -- auth -------------------------------------------------------------- #

    @staticmethod
    def _authenticate(
        instance_url: str | None, access_token: str | None
    ) -> tuple[str, str]:
        instance_url = instance_url or os.getenv("SF_INSTANCE_URL")
        access_token = access_token or os.getenv("SF_ACCESS_TOKEN")
        if instance_url and access_token:
            return instance_url.rstrip("/"), access_token

        client_id = os.getenv("SF_CLIENT_ID")
        client_secret = os.getenv("SF_CLIENT_SECRET")
        if not (client_id and client_secret):
            raise DestinationError(
                "Salesforce credentials not found. Set SF_INSTANCE_URL + SF_ACCESS_TOKEN, "
                "or SF_CLIENT_ID + SF_CLIENT_SECRET for the client credentials flow. "
                "Run with --dest dryrun to skip the write entirely."
            )

        login_url = (os.getenv("SF_LOGIN_URL") or DEFAULT_LOGIN_URL).rstrip("/")
        resp = httpx.post(
            f"{login_url}/services/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=30.0,
        )
        if resp.status_code >= 400:
            raise DestinationError(
                f"Salesforce OAuth failed ({resp.status_code}): {resp.text[:400]}"
            )
        payload = resp.json()
        return payload["instance_url"].rstrip("/"), payload["access_token"]

    # -- HTTP -------------------------------------------------------------- #

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(4):
            resp = self._client.request(method, path, **kwargs)
            if resp.status_code in (429, 503) and attempt < 3:
                wait = float(resp.headers.get("Retry-After", 2**attempt))
                log.info("Salesforce throttled, sleeping %.1fs", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < 3:
                time.sleep(2**attempt)
                continue
            if resp.status_code >= 400:
                raise DestinationError(
                    f"Salesforce {method} {path} -> {resp.status_code}: {resp.text[:400]}"
                )
            return resp
        raise DestinationError(f"Salesforce {method} {path}: exhausted retries.")

    # -- Destination -------------------------------------------------------- #

    def find(self, record: CrmRecord) -> tuple[str, dict[str, Any]] | None:
        match_key = _check_identifier(record.match_key, "field")
        fields = sorted({"Id", match_key, *self.field_names})
        # Website values vary (http/https, trailing slash, www), so match on the
        # bare domain with LIKE rather than on an exact URL.
        needle = _soql_literal(record.match_value.split("//")[-1].strip("/"))
        soql = (
            f"SELECT {', '.join(fields)} FROM {self.object_type} "
            f"WHERE {match_key} LIKE '%{needle}%' LIMIT 1"
        )
        resp = self._request(
            "GET", f"/services/data/{API_VERSION}/query", params={"q": soql}
        )
        records = resp.json().get("records", [])
        if not records:
            return None
        hit = records[0]
        record_id = str(hit.pop("Id"))
        hit.pop("attributes", None)
        return record_id, hit

    def create(self, record: CrmRecord) -> str:
        resp = self._request(
            "POST",
            f"/services/data/{API_VERSION}/sobjects/{self.object_type}",
            json=record.properties,
        )
        return str(resp.json()["id"])

    def update(self, record_id: str, properties: dict[str, Any]) -> None:
        # PATCH to an sObject returns 204 with no body.
        self._request(
            "PATCH",
            f"/services/data/{API_VERSION}/sobjects/{self.object_type}/{record_id}",
            json=properties,
        )

    def close(self) -> None:
        self._client.close()
