import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "data"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "models"))

from consistency import VerticalSpreadViolation
from violations_db import SCHEMA, record_trigger


def _fresh_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    return conn


def test_clean_trigger_writes_one_trigger_event_and_no_violations():
    conn = _fresh_conn()
    trigger_id = record_trigger(
        conn,
        detected_at_utc="2026-08-26T10:00:00+00:00",
        underlying="NIFTY",
        expiry="2026-09-01",
        trigger_instrument_key="NSE_FO|1",
        fetch_latency_ms=120.0,
        total_latency_ms=350.0,
        violations=[],
    )
    assert trigger_id == 1

    trigger_rows = conn.execute("SELECT * FROM trigger_events").fetchall()
    assert len(trigger_rows) == 1
    assert trigger_rows[0][-1] == 0  # violation_count

    violation_rows = conn.execute("SELECT * FROM detected_violations").fetchall()
    assert violation_rows == []


def test_trigger_with_violations_writes_one_row_per_violation():
    conn = _fresh_conn()
    v1 = VerticalSpreadViolation(dominant_key="A", dominated_key="B", option_type="CE", edge_paise=500)
    v2 = VerticalSpreadViolation(dominant_key="C", dominated_key="D", option_type="PE", edge_paise=200)

    trigger_id = record_trigger(
        conn,
        detected_at_utc="2026-08-26T10:00:00+00:00",
        underlying="BANKNIFTY",
        expiry="2026-09-29",
        trigger_instrument_key="NSE_FO|2",
        fetch_latency_ms=200.0,
        total_latency_ms=600.0,
        violations=[("vertical_spread", v1), ("vertical_spread", v2)],
    )

    rows = conn.execute(
        "SELECT trigger_event_id, violation_type, edge_paise, detail_json FROM detected_violations ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert all(r[0] == trigger_id for r in rows)
    assert [r[2] for r in rows] == [500, 200]

    detail = json.loads(rows[0][3])
    assert detail["dominant_key"] == "A"
    assert detail["edge_paise"] == 500


def test_multiple_triggers_get_distinct_ids_and_accumulate():
    conn = _fresh_conn()
    for i in range(3):
        record_trigger(
            conn,
            detected_at_utc=f"2026-08-26T10:0{i}:00+00:00",
            underlying="NIFTY",
            expiry="2026-09-01",
            trigger_instrument_key=f"NSE_FO|{i}",
            fetch_latency_ms=100.0,
            total_latency_ms=300.0,
            violations=[],
        )
    rows = conn.execute("SELECT id FROM trigger_events ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [1, 2, 3]
