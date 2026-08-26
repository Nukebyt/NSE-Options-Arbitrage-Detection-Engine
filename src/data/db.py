"""SQLite schema and writes for option chain snapshots.

Prices/strikes are stored as integer paise, not rupee floats -- same
reasoning as the Kalshi-phase integer-cents decision (BUG-1 in BUGS.md):
float rounding errors compound badly in an exact-equality check like put-call
parity (FOUNDATIONS.md S10, S35).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "snapshots.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS option_snapshots (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    underlying            TEXT NOT NULL,
    expiry                TEXT NOT NULL,
    strike_paise          INTEGER NOT NULL,
    option_type           TEXT NOT NULL CHECK (option_type IN ('CE', 'PE')),
    instrument_key        TEXT NOT NULL,
    timestamp_utc         TEXT NOT NULL,
    bid_paise             INTEGER NOT NULL,
    ask_paise             INTEGER NOT NULL,
    ltp_paise             INTEGER NOT NULL,
    oi                    REAL NOT NULL,
    volume                REAL NOT NULL,
    iv                    REAL,
    underlying_spot_paise INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_underlying_expiry_time
    ON option_snapshots (underlying, expiry, timestamp_utc);

CREATE INDEX IF NOT EXISTS idx_snapshots_instrument_time
    ON option_snapshots (instrument_key, timestamp_utc);
"""


def rupees_to_paise(rupees: float) -> int:
    return round(rupees * 100)


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    # WAL mode: lets a reader (e.g. the Streamlit dashboard) query this file
    # concurrently with a writer (the poller or realtime_hybrid.py) without
    # hitting "database is locked" -- the default rollback-journal mode
    # serializes readers behind writers, which matters now that this DB has
    # a live concurrent reader for the first time.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def insert_snapshots(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO option_snapshots (
            underlying, expiry, strike_paise, option_type, instrument_key, timestamp_utc,
            bid_paise, ask_paise, ltp_paise, oi, volume, iv, underlying_spot_paise
        ) VALUES (
            :underlying, :expiry, :strike_paise, :option_type, :instrument_key, :timestamp_utc,
            :bid_paise, :ask_paise, :ltp_paise, :oi, :volume, :iv, :underlying_spot_paise
        )
        """,
        rows,
    )
    conn.commit()
