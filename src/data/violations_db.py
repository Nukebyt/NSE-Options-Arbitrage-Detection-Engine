"""SQLite schema and writes for the real-time hybrid pipeline's output
(Phase 4, DEC-7's ltpc-trigger hybrid) -- logs pipeline activity/results
(every trigger, every violation found), not raw market data. Separate schema
from db.py's option_snapshots, but shares the same on-disk database file
(`data/snapshots.db`) so there's still one source of truth, and so the
Streamlit dashboard only has to open one file.

Every trigger is recorded, even when it finds zero violations -- that's the
difference between "the pipeline is alive and just isn't finding anything"
and "the pipeline stopped running," a distinction BUGS.md BUG-7 already
established matters (frozen/stale data looked identical to real quiet data
until someone checked).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from db import get_connection as _get_base_connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS trigger_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at_utc         TEXT NOT NULL,
    underlying              TEXT NOT NULL,
    expiry                  TEXT NOT NULL,
    trigger_instrument_key  TEXT NOT NULL,
    fetch_latency_ms        REAL NOT NULL,
    total_latency_ms        REAL NOT NULL,
    violation_count         INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trigger_events_time
    ON trigger_events (detected_at_utc);

CREATE TABLE IF NOT EXISTS detected_violations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_event_id   INTEGER NOT NULL REFERENCES trigger_events(id),
    detected_at_utc    TEXT NOT NULL,
    underlying         TEXT NOT NULL,
    expiry             TEXT NOT NULL,
    violation_type     TEXT NOT NULL,
    edge_paise         INTEGER NOT NULL,
    detail_json        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_detected_violations_time
    ON detected_violations (detected_at_utc);
"""


def get_connection() -> sqlite3.Connection:
    conn = _get_base_connection()
    conn.executescript(SCHEMA)
    return conn


def record_trigger(
    conn: sqlite3.Connection,
    *,
    detected_at_utc: str,
    underlying: str,
    expiry: str,
    trigger_instrument_key: str,
    fetch_latency_ms: float,
    total_latency_ms: float,
    violations: list[tuple[str, object]],
) -> int:
    """Writes one trigger_event row, plus one detected_violations row per
    violation found (zero rows if the group was clean). Returns the new
    trigger_event's id."""
    cur = conn.execute(
        """
        INSERT INTO trigger_events (
            detected_at_utc, underlying, expiry, trigger_instrument_key,
            fetch_latency_ms, total_latency_ms, violation_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            detected_at_utc, underlying, expiry, trigger_instrument_key,
            fetch_latency_ms, total_latency_ms, len(violations),
        ),
    )
    trigger_event_id = cur.lastrowid

    if violations:
        conn.executemany(
            """
            INSERT INTO detected_violations (
                trigger_event_id, detected_at_utc, underlying, expiry,
                violation_type, edge_paise, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    trigger_event_id, detected_at_utc, underlying, expiry,
                    vtype, v.edge_paise, json.dumps(asdict(v)),
                )
                for vtype, v in violations
            ],
        )

    conn.commit()
    return trigger_event_id
