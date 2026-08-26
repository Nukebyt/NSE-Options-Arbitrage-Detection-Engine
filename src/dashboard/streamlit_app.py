"""Phase 4's last checklist item: an alert/log view beyond console logging.

Deliberately decoupled from realtime_hybrid.py, not a thread/async hack
bolted onto it: the hybrid pipeline (running under tmux, same pattern as
poll.py) is the *producer*, writing every trigger and violation to
data/snapshots.db (violations_db.py) as it runs; this dashboard is a
read-only *consumer* of that same file, on a plain rerun-on-timer loop.
That split means the dashboard can be started, stopped, or crash without
ever affecting data collection -- and it works (showing an honest "nothing
yet" state) even before the pipeline has ever run once.

Visual design: a warm-neutral/charcoal palette with a single rust accent
(no purple gradients, no glassmorphism), a serif display face for headers
against monospace for all numeric data (tabular figures matter when you're
reading latency and edge sizes), and an asymmetric layout -- a dark sidebar
carries context/controls, the light main area stays dense with data rather
than the standard centered-hero-plus-three-cards template.

Run with: streamlit run src/dashboard/streamlit_app.py
"""
from __future__ import annotations

import html
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data"))

from fetch_option_chain import get_market_status  # noqa: E402
from violations_db import get_connection  # noqa: E402

st.set_page_config(page_title="NSE Options Arbitrage Monitor", layout="wide")

REFRESH_SECONDS = 5

_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,500;8..60,600;8..60,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

:root {
    --accent: #C4622D;
    --ink: #16161A;
    --paper: #F7F5F1;
    --line: #D9D4C9;
    --muted: #6B675F;
    --sidebar-bg: #121212;
    --sidebar-line: #2A2A28;
    --sidebar-text: #EDEAE3;
    --ok: #3E6B4F;
    --stale: #A6432B;
}

html, body, [data-testid="stAppViewContainer"] {
    background-color: var(--paper) !important;
    color: var(--ink) !important;
    font-family: 'IBM Plex Sans', -apple-system, sans-serif !important;
}

[data-testid="stMainBlockContainer"], .block-container {
    padding-top: 2.25rem !important;
    max-width: 1220px;
}

h1, h2, h3,
[data-testid="stMarkdownContainer"] h1,
[data-testid="stMarkdownContainer"] h2,
[data-testid="stMarkdownContainer"] h3 {
    font-family: 'Source Serif 4', Georgia, serif !important;
    font-weight: 600 !important;
    letter-spacing: -0.01em;
    color: var(--ink) !important;
}

[data-testid="stCaptionContainer"], .app-caption {
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 0.8rem !important;
    color: var(--muted) !important;
}

/* -- sidebar: dark, carries context + controls, not data -- */
[data-testid="stSidebar"] {
    background-color: var(--sidebar-bg) !important;
    border-right: 1px solid var(--sidebar-line);
}
[data-testid="stSidebar"] * { color: var(--sidebar-text) !important; }
[data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {
    font-family: 'Source Serif 4', Georgia, serif !important;
    color: #FFFFFF !important;
}
[data-testid="stSidebar"] hr { border-color: var(--sidebar-line) !important; }
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p { color: var(--sidebar-text) !important; line-height: 1.55; }
/* inline `code` spans keep Streamlit's default light chip background;
   forcing all sidebar text to sidebar-text (off-white) above left that
   background unchanged, producing near-invisible white-on-white blocks --
   give code its own dark background + accent text instead. */
[data-testid="stSidebar"] code {
    background-color: #1E1E1C !important;
    color: var(--accent) !important;
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 0.85em;
    padding: 0.08em 0.35em;
    border-radius: 2px;
}
[data-testid="stSidebar"] [data-testid="stButton"] button {
    color: var(--sidebar-text) !important;
    border-color: var(--sidebar-text) !important;
    background-color: transparent !important;
}
[data-testid="stSidebar"] [data-testid="stButton"] button:hover {
    background-color: var(--accent) !important;
    border-color: var(--accent) !important;
    color: #FFFFFF !important;
}

/* -- flat, bordered buttons: no pill shape, no shadow -- */
[data-testid="stButton"] button {
    background-color: transparent !important;
    color: var(--ink) !important;
    border: 1px solid var(--ink) !important;
    border-radius: 3px !important;
    box-shadow: none !important;
    font-family: 'IBM Plex Sans', sans-serif !important;
    font-weight: 500;
    transition: background-color 0.15s ease, border-color 0.15s ease, color 0.15s ease;
}
[data-testid="stButton"] button:hover {
    background-color: var(--accent) !important;
    border-color: var(--accent) !important;
    color: #FFFFFF !important;
}

[data-testid="stDataFrame"], [data-testid="stExpander"] {
    border: 1px solid var(--line) !important;
    border-radius: 2px !important;
    box-shadow: none !important;
}

hr { border-color: var(--line) !important; }

/* -- stat grid: deliberately asymmetric column widths -- */
.stat-grid {
    display: grid;
    grid-template-columns: 1.6fr 1fr 1fr 1fr 1fr;
    border: 1px solid var(--line);
    gap: 1px;
    background-color: var(--line);
    margin: 1.5rem 0 2.25rem 0;
}
.stat-card { background-color: var(--paper); padding: 0.95rem 1.1rem; }
.stat-card .stat-label {
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    color: var(--muted);
    margin-bottom: 0.45rem;
}
.stat-card .stat-value {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 1.5rem;
    font-variant-numeric: tabular-nums;
    color: var(--ink);
}
.stat-card .stat-sub {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.7rem;
    color: var(--muted);
    margin-top: 0.3rem;
}

.status-dot {
    display: inline-block;
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    margin-right: 0.4rem;
}
</style>
"""


def _inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def _db_connection():
    # Deliberately NOT cached with @st.cache_resource: Streamlit reruns the
    # script on a thread from an internal pool, and a different thread can
    # run the "Refresh now" / auto-refresh rerun than ran the first load.
    # sqlite3.Connection objects refuse to be used outside the thread that
    # created them (`ProgrammingError: SQLite objects created in a thread
    # can only be used in that same thread`) -- caching the connection as a
    # single shared resource is exactly what triggered that. A fresh
    # connection is opened every rerun instead; sqlite3.connect() against an
    # already-WAL-enabled file is cheap, and this dashboard only ever reads
    # a handful of small, LIMIT-bounded queries.
    return get_connection()


def _load_triggers(conn, limit: int = 500) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM trigger_events ORDER BY id DESC LIMIT ?", conn, params=(limit,)
    )


def _load_violations(conn, limit: int = 200) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM detected_violations ORDER BY id DESC LIMIT ?", conn, params=(limit,)
    )


def _stat_card(label: str, value: str, sub: str = "", value_color: str | None = None) -> str:
    # No embedded newlines/indentation: Streamlit's markdown renderer treats
    # a whitespace-only line inside an unsafe_allow_html block as a blank
    # line, which ends CommonMark's raw-HTML-block mode -- everything after
    # that point (including subsequent stat cards) then gets reparsed as
    # plain markdown, where a 4-space-indented line is an indented code
    # block, not HTML. That's what produced literal "<div>...</div>" text
    # for every card after the first one. Keeping each card's HTML on a
    # single line with no blank lines between concatenated cards avoids the
    # whole failure mode.
    style = f' style="color:{value_color}"' if value_color else ""
    return (
        '<div class="stat-card">'
        f'<div class="stat-label">{html.escape(label)}</div>'
        f'<div class="stat-value"{style}>{html.escape(value)}</div>'
        f'<div class="stat-sub">{html.escape(sub)}</div>'
        "</div>"
    )


def _render_sidebar() -> bool:
    """Renders sidebar content (about copy, market status, controls).
    Returns whether auto-refresh is enabled."""
    with st.sidebar:
        st.markdown("### NSE Options Arbitrage Monitor")
        st.markdown(
            "A live scan of NIFTY/BANKNIFTY index options for prices that "
            "briefly break basic no-arbitrage rules -- relationships that "
            "must hold between related option prices regardless of where "
            "the market thinks the index is headed."
        )

        st.markdown("---")
        st.markdown("**How the data is collected**")
        st.markdown(
            "Upstox's WebSocket feed only streams full bid/ask depth to "
            "paid-tier accounts, so this project uses a hybrid instead. It "
            "subscribes to the free `ltpc` (last-traded-price) stream "
            "purely as a **trigger** -- any trade on a tracked option "
            "signals \"this expiry's book may have moved\" -- then fetches "
            "that expiry's real, executable bid/ask over the REST "
            "option-chain API and runs the checks against *that*, never "
            "against the trigger's LTP. A 2-second debounce stops one busy "
            "strike from re-triggering a fetch for its whole group many "
            "times a second."
        )

        st.markdown("**The three checks**")
        st.markdown(
            "- `vertical_spread` -- a better-strike option can't be priced "
            "below a worse-strike one\n"
            "- `put_call_parity` -- call, put, and underlying must imply "
            "the same forward\n"
            "- `convexity` -- a middle strike can't cost more than its "
            "neighbors' average (the butterfly bound)"
        )

        st.markdown("**`edge_paise`**")
        st.markdown(
            "Violation size in paise (1 rupee = 100 paise), computed from "
            "real bid/ask, before transaction costs and before checking "
            "open interest. A nonzero edge means the prices are briefly "
            "inconsistent -- not that it's a free, executable profit."
        )

        st.markdown("---")
        try:
            status = get_market_status()
            dot_color = "var(--ok)" if status == "NORMAL_OPEN" else "var(--stale)"
            st.markdown(
                f'<span class="status-dot" style="background-color:{dot_color}"></span>'
                f'<span style="font-family:\'IBM Plex Mono\',monospace;font-size:0.85rem">{html.escape(status)}</span>',
                unsafe_allow_html=True,
            )
        except Exception as e:
            st.markdown(f"Market status unknown ({html.escape(str(e))})")

        auto_refresh = st.checkbox("Auto-refresh (5s)", value=False)

    return auto_refresh


def main() -> None:
    _inject_css()
    auto_refresh = _render_sidebar()

    st.title("Live Detection Feed")
    st.caption("BUGS.md DEC-7 / DEC-8 -- ROADMAP.md Phase 4")

    with closing(_db_connection()) as conn:
        triggers = _load_triggers(conn)
        violations = _load_violations(conn)

    if triggers.empty:
        st.info(
            "No trigger events yet. This dashboard only displays what "
            "realtime_hybrid.py has written -- start it (during market "
            "hours) to see live data here."
        )
        _maybe_autorefresh(auto_refresh)
        return

    last_trigger_utc = pd.to_datetime(triggers["detected_at_utc"].iloc[0])
    seconds_since_last = (datetime.now(timezone.utc) - last_trigger_utc.to_pydatetime()).total_seconds()
    pipeline_alive = seconds_since_last < 60

    stat_html = '<div class="stat-grid">'
    stat_html += _stat_card(
        "Pipeline",
        "Alive" if pipeline_alive else "Stale",
        f"last trigger {seconds_since_last:.0f}s ago",
        value_color="var(--ok)" if pipeline_alive else "var(--stale)",
    )
    stat_html += _stat_card("Triggers logged", str(len(triggers)))
    stat_html += _stat_card("Violations found", str(int(triggers["violation_count"].sum())))
    stat_html += _stat_card("Median fetch latency", f"{triggers['fetch_latency_ms'].median():.0f} ms")
    stat_html += _stat_card("Median tick-to-detection", f"{triggers['total_latency_ms'].median():.0f} ms")
    stat_html += "</div>"
    st.markdown(stat_html, unsafe_allow_html=True)

    col_header, col_btn = st.columns([5, 1])
    with col_header:
        st.subheader("Recent violations")
    with col_btn:
        st.write("")
        st.button("Refresh now")

    if violations.empty:
        st.write("No violations in the current window -- consistent with real markets being mostly efficient (FOUNDATIONS.md S18).")
    else:
        display_cols = ["detected_at_utc", "underlying", "expiry", "violation_type", "edge_paise", "detail_json"]
        st.dataframe(violations[display_cols], width="stretch", hide_index=True, height=420)

    col_group, col_type = st.columns([1, 1])

    with col_group:
        st.subheader("By group")
        by_group = triggers.groupby(["underlying", "expiry"]).size().rename("triggers")
        st.dataframe(by_group.reset_index(), width="stretch", hide_index=True)

    with col_type:
        st.subheader("By violation type")
        if violations.empty:
            st.write("No violations yet.")
        else:
            by_type = violations.groupby("violation_type")["edge_paise"].agg(["count", "mean", "max"])
            by_type.columns = ["count", "mean_edge_paise", "max_edge_paise"]
            st.bar_chart(by_type["count"])

    st.subheader("Tick-to-detection latency")
    latency_chart = triggers.sort_values("id")[["id", "fetch_latency_ms", "total_latency_ms"]].set_index("id")
    st.line_chart(latency_chart)

    _maybe_autorefresh(auto_refresh)


def _maybe_autorefresh(enabled: bool) -> None:
    if enabled:
        time.sleep(REFRESH_SECONDS)
        st.rerun()


if __name__ == "__main__":
    main()
