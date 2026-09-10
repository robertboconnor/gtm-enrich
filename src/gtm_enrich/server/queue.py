"""A durable job queue in SQLite.

Webhooks need an answer in seconds; enrichment takes twenty or more. So the
endpoint's only job is to write the event down and return 200. Something else
does the work.

SQLite rather than Redis because it needs no second service to deploy, and the
job table survives a restart -- an in-memory queue silently loses everything
in flight when the container recycles, which is exactly when you notice. A
deployment running many workers would point this at Postgres; `claim` is written
as a single atomic UPDATE so the swap is a connection-string change rather than
a redesign.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_QUEUE_PATH = Path(".cache") / "queue.db"
MAX_ATTEMPTS = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    run_after    TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_error   TEXT
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (status, run_after);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Job:
    id: int
    payload: dict[str, Any]
    attempts: int


class JobQueue:
    def __init__(self, path: Path | str = DEFAULT_QUEUE_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        finally:
            conn.close()

    def enqueue(self, payload: dict[str, Any], delay_seconds: float = 0.0) -> int:
        """Add a job. `delay_seconds` defers it -- used when a record arrives empty."""
        run_after = utcnow() + timedelta(seconds=delay_seconds)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO jobs (payload, run_after, created_at) VALUES (?, ?, ?)",
                (json.dumps(payload), run_after.isoformat(), utcnow().isoformat()),
            )
            return int(cursor.lastrowid)

    def claim(self) -> Job | None:
        """Take the next ready job, atomically.

        One UPDATE marks it running and hands it back, so two workers polling the
        same table cannot both claim the same row.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1 "
                "WHERE id = (SELECT id FROM jobs WHERE status = 'pending' "
                "  AND run_after <= ? ORDER BY id LIMIT 1) "
                "RETURNING id, payload, attempts",
                (utcnow().isoformat(),),
            ).fetchone()
        if row is None:
            return None
        return Job(id=int(row[0]), payload=json.loads(row[1]), attempts=int(row[2]))

    def complete(self, job_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (job_id,))

    def fail(self, job_id: int, error: str, retry_in_seconds: float = 30.0) -> None:
        """Retry with a delay, or give up and leave the row for inspection."""
        with self._lock, self._connect() as conn:
            attempts = conn.execute(
                "SELECT attempts FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            attempts = int(attempts[0]) if attempts else MAX_ATTEMPTS
            if attempts >= MAX_ATTEMPTS:
                # Dead-lettered rather than deleted: a failed job you cannot read
                # afterwards is a failed job you cannot debug.
                conn.execute(
                    "UPDATE jobs SET status = 'failed', last_error = ? WHERE id = ?",
                    (error[:500], job_id),
                )
            else:
                run_after = utcnow() + timedelta(seconds=retry_in_seconds * attempts)
                conn.execute(
                    "UPDATE jobs SET status = 'pending', run_after = ?, last_error = ? "
                    "WHERE id = ?",
                    (run_after.isoformat(), error[:500], job_id),
                )

    def stats(self) -> dict[str, int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM jobs GROUP BY status"
            ).fetchall()
        return {status: int(count) for status, count in rows}
