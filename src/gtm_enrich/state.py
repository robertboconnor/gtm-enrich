"""Run state: what we've already done, so scheduled runs stay incremental.

Two things get remembered, both in one small SQLite file.

**Watermarks.** When a scheduled run finishes, it records when it started. The
next run can ask for records modified since then instead of re-reading the whole
database. The watermark is the *start* time, not the finish time, so records
changed while a run was in flight are picked up next time rather than skipped.

**Seen events.** Webhooks retry and duplicate -- the same account creation can
arrive three times. Each event gets a key, and a key already seen is dropped.
Without this, real-time enrichment bills you several times for one record.

SQLite because it needs no server, survives a restart, and is safe across the
handful of processes this ever runs. A deployment with many workers would point
the same code at Postgres; the interface would not change.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_STATE_PATH = Path(".cache") / "state.db"
# How long a webhook event id is remembered. Comfortably longer than any
# provider's retry window.
SEEN_TTL_HOURS = 72

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watermarks (
    key        TEXT PRIMARY KEY,
    ran_at     TEXT NOT NULL,
    records    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen_events (
    key        TEXT PRIMARY KEY,
    seen_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS seen_events_seen_at ON seen_events (seen_at);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StateStore:
    """Durable run state. Safe to use from several threads in one process."""

    def __init__(self, path: Path | str = DEFAULT_STATE_PATH) -> None:
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

    # -- watermarks -------------------------------------------------------- #

    def last_run(self, key: str) -> datetime | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT ran_at FROM watermarks WHERE key = ?", (key,)
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def record_run(self, key: str, started_at: datetime, records: int = 0) -> None:
        """Store the run's *start* time.

        Using the start rather than the finish means a record modified while the
        run was in progress is caught by the next run instead of falling into
        the gap between them.
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO watermarks (key, ran_at, records) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET ran_at = excluded.ran_at, "
                "records = excluded.records",
                (key, started_at.isoformat(), records),
            )

    def watermarks(self) -> dict[str, tuple[datetime, int]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT key, ran_at, records FROM watermarks").fetchall()
        return {k: (datetime.fromisoformat(t), n) for k, t, n in rows}

    # -- event dedupe ------------------------------------------------------ #

    def mark_seen(self, key: str) -> bool:
        """Record an event key. Returns True if this is the first time we've seen it.

        The insert is the check -- doing it in one statement means two workers
        racing on the same duplicate cannot both win.
        """
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO seen_events (key, seen_at) VALUES (?, ?)",
                (key, utcnow().isoformat()),
            )
            return cursor.rowcount == 1

    def purge_seen(self, older_than_hours: int = SEEN_TTL_HOURS) -> int:
        cutoff = (utcnow() - timedelta(hours=older_than_hours)).isoformat()
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM seen_events WHERE seen_at < ?", (cutoff,))
            return cursor.rowcount
