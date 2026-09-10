"""The part that does the work, off the request path.

A webhook handler must answer in seconds; one enrichment takes twenty or more.
So the handler writes a job down and returns, and this drains the queue.

The interesting case is not enrichment -- that is the same call the CLI makes.
It is that **a record is usually created empty.** Something creates the company,
and whatever populates its website does so a second or two later. A webhook
firing on `company.creation` therefore arrives before there is anything to
scrape. Rather than dropping those, the job is re-queued with a delay and a
bounded number of retries, so the common case resolves itself and the genuinely
website-less records eventually stop costing anything.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path

from ..config import IcpProfile, MappingConfig, Settings, has_llm_credentials
from ..destinations import build_destination
from ..mapping import mapping_field_names
from ..pipeline import enrich_domains, write_results
from .queue import JobQueue

log = logging.getLogger(__name__)

# How long to wait for a just-created record to have its website filled in.
EMPTY_RECORD_RETRY_SECONDS = 90.0
MAX_EMPTY_RETRIES = 3


class Worker:
    """Drains the job queue. Run one in-process, or several as separate containers."""

    def __init__(
        self,
        queue: JobQueue,
        settings: Settings,
        icp_path: Path,
        mapping_path: Path,
        destination: str = "dryrun",
        poll_seconds: float = 2.0,
    ) -> None:
        self.queue = queue
        self.settings = settings
        self.icp = IcpProfile.load(icp_path)
        self.mapping = MappingConfig.load(mapping_path)
        self.destination_name = destination
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, daemon=True)
        self._thread.start()
        log.info("worker started, destination=%s", self.destination_name)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(self.poll_seconds)

    # -- work -------------------------------------------------------------- #

    def run_once(self) -> bool:
        """Process one job. Returns False when the queue is empty."""
        job = self.queue.claim()
        if job is None:
            return False
        try:
            self._process(job.payload, job.attempts)
            self.queue.complete(job.id)
        except Exception as exc:  # noqa: BLE001 - a bad job must not kill the worker
            log.exception("job %s failed", job.id)
            self.queue.fail(job.id, str(exc))
        return True

    def _process(self, payload: dict, attempts: int) -> None:
        domain = payload.get("domain")

        if not domain and payload.get("record_id"):
            domain = self._resolve_domain(payload)

        if not domain:
            if attempts <= MAX_EMPTY_RETRIES:
                # Almost certainly a record created moments ago whose website
                # has not been written yet. Wait and look again.
                raise RuntimeError(
                    f"record {payload.get('record_id')} has no website yet; "
                    f"retrying in {EMPTY_RECORD_RETRY_SECONDS:.0f}s"
                )
            log.info("giving up on %s: still no website", payload.get("record_id"))
            return

        use_llm = has_llm_credentials(self.settings.analyze.provider)
        results = asyncio.run(
            enrich_domains([domain], self.settings, self.icp, use_llm=use_llm)
        )

        shape = "hubspot" if self.destination_name == "dryrun" else self.destination_name
        destination = build_destination(
            self.destination_name,
            object_type=self.mapping.objects.get(shape, "companies"),
            field_names=mapping_field_names(self.mapping, shape),
            output_dir=self.settings.output_dir,
            shape=shape,
        )
        with destination:
            writes = write_results(results, self.mapping, destination, shape)
        for w in writes:
            log.info("%s -> %s (%s)", w.domain, w.action, self.destination_name)

    def _resolve_domain(self, payload: dict) -> str | None:
        """Fetch the website for a record we only know by id."""
        if payload.get("system") != "hubspot":
            return None
        from ..sources import HubSpotSource

        source = HubSpotSource()
        try:
            return source.get_domain(str(payload["record_id"]))
        finally:
            source.close()


def drain(queue: JobQueue, worker: Worker, timeout: float = 60.0) -> int:
    """Process everything currently queued. Used by tests and one-shot runs."""
    processed, deadline = 0, time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not worker.run_once():
            break
        processed += 1
    return processed
