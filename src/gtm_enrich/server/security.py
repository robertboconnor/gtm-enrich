"""Webhook authentication.

A public endpoint that triggers paid enrichment is a bill waiting to be run up
by anyone who finds the URL. Both handlers verify before doing any work.

HubSpot's v3 scheme, per their request-validation docs: HMAC-SHA256 over
`method + uri + body + timestamp`, keyed with the app's client secret,
base64-encoded, sent as `X-HubSpot-Signature-v3`, with the timestamp in
`X-HubSpot-Request-Timestamp` and a five-minute validity window.

Salesforce has no equivalent for a Flow HTTP callout, so that endpoint takes a
shared secret you configure on both ends. Weaker, and named as such.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

# HubSpot rejects anything older than five minutes; matching that closes the
# replay window rather than trusting a signature forever.
MAX_SIGNATURE_AGE_MS = 5 * 60 * 1000


class SignatureError(Exception):
    """The request could not be authenticated."""


def _constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def hubspot_signature(secret: str, method: str, uri: str, body: str, timestamp: str) -> str:
    """Compute the v3 signature. Exposed so tests can sign their own fixtures."""
    message = f"{method}{uri}{body}{timestamp}".encode()
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def verify_hubspot(
    *,
    secret: str | None,
    method: str,
    uri: str,
    body: str,
    signature: str | None,
    timestamp: str | None,
    now_ms: int | None = None,
) -> None:
    """Raise `SignatureError` unless this really came from HubSpot."""
    if not secret:
        raise SignatureError(
            "HUBSPOT_CLIENT_SECRET is not set, so webhook requests cannot be verified. "
            "Refusing to process unauthenticated events."
        )
    if not signature or not timestamp:
        raise SignatureError("Missing X-HubSpot-Signature-v3 or X-HubSpot-Request-Timestamp.")

    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise SignatureError("Malformed X-HubSpot-Request-Timestamp.") from exc

    age = (now_ms if now_ms is not None else int(time.time() * 1000)) - sent_at
    if age > MAX_SIGNATURE_AGE_MS:
        raise SignatureError(f"Request is {age // 1000}s old; the limit is 300s (replay guard).")

    expected = hubspot_signature(secret, method, uri, body, timestamp)
    if not _constant_time_equal(expected, signature):
        raise SignatureError("Signature does not match.")


def verify_shared_secret(header_value: str | None, env_var: str = "GTM_WEBHOOK_SECRET") -> None:
    """Shared-secret check for senders with no signing scheme, e.g. a Salesforce Flow."""
    expected = os.getenv(env_var)
    if not expected:
        raise SignatureError(
            f"{env_var} is not set, so this endpoint cannot authenticate callers."
        )
    if not header_value or not _constant_time_equal(expected, header_value):
        raise SignatureError("Shared secret missing or incorrect.")
