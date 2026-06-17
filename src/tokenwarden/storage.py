"""Append-only SQLite log of usage events, plus simple spend aggregates."""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from tokenwarden.models import Usage

log = logging.getLogger("tokenwarden.storage")

_UTC = ZoneInfo("UTC")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id            INTEGER PRIMARY KEY,
  ts            TEXT NOT NULL,            -- UTC ISO-8601 (sortable lexicographically)
  agent_id      TEXT NOT NULL,
  model         TEXT,
  service_tier  TEXT,
  input_tokens          INTEGER NOT NULL DEFAULT 0,
  output_tokens         INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
  cost_usd      REAL NOT NULL DEFAULT 0,  -- ESTIMATED until phase-2 reconciliation
  request_id    TEXT UNIQUE,              -- idempotency: don't double-count retries
  source        TEXT NOT NULL DEFAULT 'gateway'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_agent_ts ON events(agent_id, ts);
"""


def _day_start_utc_iso(tz: ZoneInfo) -> str:
    """ISO timestamp for local midnight today, expressed in UTC for comparison."""
    local_midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(_UTC).isoformat()


class Storage:
    def __init__(self, db_path: str) -> None:
        # check_same_thread=False because the async gateway records from arbitrary
        # task contexts; a single lock serializes the (low-volume) writes.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def record_event(
        self,
        *,
        ts: str,
        agent_id: str,
        usage: Usage,
        cost_usd: float,
        request_id: str | None,
        source: str = "gateway",
    ) -> bool:
        """Insert one usage event. Idempotent on `request_id` (INSERT OR IGNORE),
        so a retried or reconnected request is not double-counted. Returns True
        if a new row was inserted."""
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO events
                   (ts, agent_id, model, service_tier, input_tokens, output_tokens,
                    cache_creation_tokens, cache_read_tokens, cost_usd, request_id, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts,
                    agent_id,
                    usage.model,
                    usage.service_tier,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_creation_tokens,
                    usage.cache_read_tokens,
                    cost_usd,
                    request_id,
                    source,
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def spend_today(self, tz: ZoneInfo, agent_id: str | None = None) -> float:
        """Sum estimated cost since local midnight (today, in `tz`)."""
        query = "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM events WHERE ts >= ?"
        params: list = [_day_start_utc_iso(tz)]
        if agent_id is not None:
            query += " AND agent_id = ?"
            params.append(agent_id)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return float(row["s"])

    def spend_by_agent_today(self, tz: ZoneInfo) -> dict[str, float]:
        """Estimated cost per agent since local midnight, highest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT agent_id, COALESCE(SUM(cost_usd), 0) AS s FROM events "
                "WHERE ts >= ? GROUP BY agent_id ORDER BY s DESC",
                (_day_start_utc_iso(tz),),
            ).fetchall()
        return {r["agent_id"]: float(r["s"]) for r in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
