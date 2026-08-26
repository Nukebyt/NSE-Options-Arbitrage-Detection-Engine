"""Phase 3: backtest violations found across accumulated snapshot history.

Reconstructs each poll cycle's option chain state from data/snapshots.db and
re-runs Phase 2's checks against it, tracking how many cycles each specific
violation persists across, whether it stays profitable net of REAL
transaction costs (src/models/transaction_costs.py -- brokerage + STT +
exchange charges + stamp duty + SEBI charges + IPFT + GST, not just a flat
brokerage approximation), and segmenting by strike distance from spot
(moneyness) and open interest (liquidity).

CAVEAT stated explicitly rather than glossed over, and printed every run:
persistence/profitability numbers here are only as good as the accumulation
window the poller has actually run for. Early runs of this script are a
pipeline-correctness check, not a statistically meaningful conclusion -- see
FOUNDATIONS.md S23 on small-sample honesty. Re-run after days/weeks of
accumulated history for a real answer to "is this exploitable."
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "models"))

from db import DEFAULT_DB_PATH  # noqa: E402
from consistency import (  # noqa: E402
    OptionQuote,
    check_calendar_monotonic,
    check_convexity,
    check_put_call_parity,
    check_vertical_spread,
    implied_forward_paise,
)
from transaction_costs import LOT_SIZES, trade_cost_paise  # noqa: E402

RISK_FREE_RATE = 0.065


def load_snapshots(db_path: Path = DEFAULT_DB_PATH) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM option_snapshots ORDER BY timestamp_utc").fetchall()
    conn.close()
    return rows


def group_by_cycle(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    cycles = defaultdict(list)
    for r in rows:
        cycles[r["timestamp_utc"]].append(r)
    return dict(sorted(cycles.items()))


def scan_cycle(rows_for_cycle: list[sqlite3.Row]) -> list[dict]:
    """Returns one dict per detected violation instance for this cycle:
    key, type, edge_paise, legs ([(side, premium_paise), ...]), underlying,
    strike_paise (representative, for moneyness), spot_paise, min_oi."""
    by_underlying_expiry = defaultdict(list)
    for r in rows_for_cycle:
        by_underlying_expiry[(r["underlying"], r["expiry"])].append(r)

    # For the calendar check (FOUNDATIONS.md S17): same strike/option_type,
    # compared ACROSS expiries within one underlying.
    by_underlying_strike_type: dict[tuple, dict[str, tuple[OptionQuote, float]]] = defaultdict(dict)
    global_oi_by_key: dict[str, float] = {}

    results: list[dict] = []
    for (underlying, expiry), group_rows in by_underlying_expiry.items():
        calls: dict[int, OptionQuote] = {}
        puts: dict[int, OptionQuote] = {}
        oi_by_key: dict[str, float] = {}
        spot_paise = group_rows[0]["underlying_spot_paise"]

        for r in group_rows:
            # Defense in depth against BUG-5 (a one-sided quote with a missing
            # side stored as 0). Old rows collected before that fix can still
            # be sitting in the DB; don't trust the poller alone to have kept
            # them out.
            if r["bid_paise"] <= 0 or r["ask_paise"] <= 0:
                continue
            quote = OptionQuote(r["instrument_key"], r["option_type"], r["bid_paise"], r["ask_paise"])
            (calls if r["option_type"] == "CE" else puts)[r["strike_paise"]] = quote
            oi_by_key[r["instrument_key"]] = r["oi"]
            global_oi_by_key[r["instrument_key"]] = r["oi"]
            by_underlying_strike_type[(underlying, r["strike_paise"], r["option_type"])][expiry] = (quote, spot_paise)

        for label, book in (("CE", calls), ("PE", puts)):
            for v in check_vertical_spread(list(book.items()), label):
                key = (underlying, expiry, label, "vertical", v.dominant_key, v.dominated_key)
                strike = next(k for k, q in book.items() if q.instrument_key == v.dominated_key)
                dominant_q = next(q for q in book.values() if q.instrument_key == v.dominant_key)
                dominated_q = next(q for q in book.values() if q.instrument_key == v.dominated_key)
                legs = [("buy", dominant_q.ask_paise), ("sell", dominated_q.bid_paise)]
                min_oi = min(oi_by_key.get(v.dominant_key, 0), oi_by_key.get(v.dominated_key, 0))
                results.append(dict(key=key, type="vertical_spread", edge_paise=v.edge_paise, legs=legs,
                                     underlying=underlying, strike_paise=strike, spot_paise=spot_paise, min_oi=min_oi))

        common_strikes = sorted(set(calls) & set(puts))
        if common_strikes:
            cycle_date = datetime.fromisoformat(group_rows[0]["timestamp_utc"]).date()
            expiry_date = datetime.fromisoformat(expiry).date()
            years_to_expiry = max((expiry_date - cycle_date).days, 0) / 365.0
            atm_strike = min(common_strikes, key=lambda k: abs(k - spot_paise))
            forward = implied_forward_paise(calls[atm_strike], puts[atm_strike], atm_strike, RISK_FREE_RATE, years_to_expiry)
            for strike in common_strikes:
                v = check_put_call_parity(calls[strike], puts[strike], forward, strike, RISK_FREE_RATE, years_to_expiry)
                if v:
                    key = (underlying, expiry, "parity", strike)
                    if v.direction == "synthetic_long_forward_cheap":
                        legs = [("buy", calls[strike].ask_paise), ("sell", puts[strike].bid_paise)]
                    else:
                        legs = [("sell", calls[strike].bid_paise), ("buy", puts[strike].ask_paise)]
                    min_oi = min(oi_by_key.get(calls[strike].instrument_key, 0), oi_by_key.get(puts[strike].instrument_key, 0))
                    results.append(dict(key=key, type="put_call_parity", edge_paise=v.edge_paise, legs=legs,
                                         underlying=underlying, strike_paise=strike, spot_paise=spot_paise, min_oi=min_oi))

        for label, book in (("CE", calls), ("PE", puts)):
            strikes = sorted(book)
            for i in range(len(strikes) - 2):
                k1, k2, k3 = strikes[i], strikes[i + 1], strikes[i + 2]
                if (k2 - k1) != (k3 - k2):
                    continue
                v = check_convexity(book[k1], book[k2], book[k3], k1, k2, k3)
                if v:
                    key = (underlying, expiry, label, "convexity", k1, k2, k3)
                    legs = [("buy", book[k1].ask_paise), ("sell", book[k2].bid_paise), ("sell", book[k2].bid_paise), ("buy", book[k3].ask_paise)]
                    min_oi = min(oi_by_key.get(book[k1].instrument_key, 0), oi_by_key.get(book[k2].instrument_key, 0), oi_by_key.get(book[k3].instrument_key, 0))
                    results.append(dict(key=key, type="convexity", edge_paise=v.edge_paise, legs=legs,
                                         underlying=underlying, strike_paise=k2, spot_paise=spot_paise, min_oi=min_oi))

    for (underlying, strike_paise, option_type), by_expiry in by_underlying_strike_type.items():
        expiries = sorted(by_expiry)
        for near_expiry, far_expiry in zip(expiries, expiries[1:]):
            near_quote, spot_paise = by_expiry[near_expiry]
            far_quote, _ = by_expiry[far_expiry]
            v = check_calendar_monotonic(near_quote, near_expiry, far_quote, far_expiry, strike_paise)
            if v:
                key = (underlying, option_type, "calendar", strike_paise, near_expiry, far_expiry)
                legs = [("sell", near_quote.bid_paise), ("buy", far_quote.ask_paise)]
                min_oi = min(global_oi_by_key.get(near_quote.instrument_key, 0), global_oi_by_key.get(far_quote.instrument_key, 0))
                results.append(dict(key=key, type="calendar", edge_paise=v.edge_paise, legs=legs,
                                     underlying=underlying, strike_paise=strike_paise, spot_paise=spot_paise, min_oi=min_oi))

    return results


def moneyness_bucket(strike_paise: int, spot_paise: int) -> str:
    pct = abs(strike_paise - spot_paise) / spot_paise * 100
    if pct < 2:
        return "near_atm (<2%)"
    if pct < 5:
        return "mid (2-5%)"
    return "far (>5%)"


def run_backtest() -> None:
    rows = load_snapshots()
    cycles = group_by_cycle(rows)
    timestamps = list(cycles.keys())

    if not timestamps:
        print("No snapshot data found -- let the poller run first (see ROADMAP.md Phase 1).")
        return

    history: dict[tuple, list[dict]] = defaultdict(list)
    type_counts: dict[str, int] = defaultdict(int)

    for ts in timestamps:
        for v in scan_cycle(cycles[ts]):
            v["timestamp"] = ts
            history[v["key"]].append(v)
            type_counts[v["type"]] += 1

    all_instances = [v for occurrences in history.values() for v in occurrences]
    total_instances = len(all_instances)

    # LOW_OI_FLOOR is not a rigorous liquidity model, just a blunt "someone is
    # actually there" sanity bar -- see the correlation this run actually
    # found (printed below) rather than trust this number in isolation.
    LOW_OI_FLOOR = 500

    net_profitable = 0
    net_profitable_low_oi = 0
    for v in all_instances:
        lot_size = LOT_SIZES.get(v["underlying"], 1)
        cost = trade_cost_paise(v["legs"], lot_size)
        # edge_paise is per-unit; scale to one lot to compare against real,
        # lot-sized transaction costs rather than mixing per-unit and per-lot units.
        gross_paise = v["edge_paise"] * lot_size
        v["net_paise"] = gross_paise - cost
        if v["net_paise"] > 0:
            net_profitable += 1
            if v["min_oi"] is not None and v["min_oi"] < LOW_OI_FLOOR:
                net_profitable_low_oi += 1

    persistence_seconds = []
    for occurrences in history.values():
        occ_times = [datetime.fromisoformat(v["timestamp"]) for v in occurrences]
        if len(occ_times) >= 2:
            persistence_seconds.append((max(occ_times) - min(occ_times)).total_seconds())

    print(f"Poll cycles analyzed: {len(timestamps)} (span: {timestamps[0]} to {timestamps[-1]})")
    print(f"Distinct violation identities seen: {len(history)}")
    print(f"Total violation instances (summed across cycles): {total_instances}")
    print(f"By type: {dict(type_counts)}")
    print()
    print("Net of REAL transaction costs (brokerage + STT + exchange charges + stamp duty + SEBI + IPFT + GST,")
    print("per src/models/transaction_costs.py, verified rates as of 2026-08-25 -- FOUNDATIONS.md S8):")
    print(f"  {net_profitable}/{total_instances} instances stayed net-positive per lot after ALL real costs")
    if net_profitable and net_profitable_low_oi:
        print(f"  WARNING: {net_profitable_low_oi}/{net_profitable} of those net-positive instances have")
        print(f"  min-leg open interest under {LOW_OI_FLOOR} -- likely unexecutable at the quoted size, not real")
        print("  edge. 'Net-positive after costs' is necessary but NOT sufficient for real exploitability;")
        print("  see the largest edges specifically before believing any of this is tradeable.")
        biggest = max(all_instances, key=lambda v: v["net_paise"])
        print(f"  Largest net-positive: {biggest['type']} on {biggest['underlying']}, net=Rs{biggest['net_paise']/100:.2f}, "
              f"min_oi={biggest['min_oi']} -- {'THIN, treat as noise' if (biggest['min_oi'] or 0) < LOW_OI_FLOOR else 'liquid enough to take seriously'}")
    print()

    if all_instances:
        print("By moneyness (strike distance from spot):")
        by_money = defaultdict(int)
        for v in all_instances:
            by_money[moneyness_bucket(v["strike_paise"], v["spot_paise"])] += 1
        for bucket, count in sorted(by_money.items()):
            print(f"  {bucket}: {count}")

        liquid = [v for v in all_instances if v["min_oi"] is not None]
        if liquid:
            ois = sorted(v["min_oi"] for v in liquid)
            median_oi = ois[len(ois) // 2]
            print(f"\nBy liquidity (median min-leg OI this run: {median_oi:.0f}):")
            above = sum(1 for v in liquid if v["min_oi"] >= median_oi)
            below = len(liquid) - above
            print(f"  at/above median OI: {above}   below median OI: {below}")
        print()

    if persistence_seconds:
        persistence_seconds.sort()
        n = len(persistence_seconds)
        print(f"Persistence, violations seen in >=2 cycles (n={n} -- SMALL SAMPLE, FOUNDATIONS.md S23):")
        print(f"  median {persistence_seconds[n // 2]:.0f}s, min {persistence_seconds[0]:.0f}s, max {persistence_seconds[-1]:.0f}s")
    else:
        print("No violation has yet persisted across multiple poll cycles (short accumulation window so far).")

    print()
    print(f"CAVEAT: {len(timestamps)} poll cycles is a short accumulation window.")
    print("These numbers demonstrate the pipeline is working correctly, not yet a statistically")
    print("meaningful answer to 'is this exploitable' -- re-run after days/weeks of history.")

    _write_charts(history, timestamps, all_instances, LOW_OI_FLOOR)


def _write_charts(
    history: dict[tuple, list[dict]],
    timestamps: list[str],
    all_instances: list[dict],
    low_oi_floor: int,
) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("\n(matplotlib not available -- skipping charts)")
        return

    charts_dir = Path(__file__).resolve().parents[2] / "data" / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)

    counts_per_cycle = defaultdict(int)
    for occurrences in history.values():
        for v in occurrences:
            counts_per_cycle[v["timestamp"]] += 1
    counts = [counts_per_cycle.get(ts, 0) for ts in timestamps]

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.bar(range(len(timestamps)), counts)
    ax.set_xlabel("poll cycle (chronological)")
    ax.set_ylabel("violations found")
    ax.set_title("Violations per poll cycle")
    fig.tight_layout()
    fig.savefig(charts_dir / "violations_per_cycle.png")
    plt.close(fig)

    all_edges_rupees = [v["edge_paise"] / 100 for occurrences in history.values() for v in occurrences]
    if all_edges_rupees:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.hist(all_edges_rupees, bins=min(20, len(all_edges_rupees)))
        ax.set_xlabel("edge (Rs, per unit)")
        ax.set_ylabel("count")
        ax.set_title("Detected violation edge size distribution")
        fig.tight_layout()
        fig.savefig(charts_dir / "edge_distribution.png")
        plt.close(fig)

    # Third chart: DEC-5's finding made visual -- "net-positive after real
    # costs" is necessary but not sufficient for real exploitability, since
    # a chunk of net-positive instances are on legs nobody is actually
    # quoting (min_oi under the floor). Same restrained palette as the
    # Streamlit dashboard (src/dashboard/streamlit_app.py) for visual
    # consistency across the project's deliverables.
    if all_instances:
        not_profitable = sum(1 for v in all_instances if v["net_paise"] <= 0)
        profitable_thin = sum(
            1 for v in all_instances
            if v["net_paise"] > 0 and v["min_oi"] is not None and v["min_oi"] < low_oi_floor
        )
        profitable_liquid = sum(
            1 for v in all_instances
            if v["net_paise"] > 0 and not (v["min_oi"] is not None and v["min_oi"] < low_oi_floor)
        )

        fig, ax = plt.subplots(figsize=(7, 4))
        labels = [
            "net-negative\n(costs erase edge)",
            f"net-positive,\nthin (OI < {low_oi_floor})",
            "net-positive,\nliquid enough",
        ]
        values = [not_profitable, profitable_thin, profitable_liquid]
        colors = ["#8A8578", "#C4622D", "#3E6B4F"]
        ax.bar(labels, values, color=colors)
        ax.set_ylabel("violation instances")
        ax.set_title("Net profitability after real transaction costs")
        fig.tight_layout()
        fig.savefig(charts_dir / "net_profitability.png")
        plt.close(fig)

    # Fourth chart: violations by moneyness bucket -- same segmentation
    # already printed to console, made visual. Distance from spot is a
    # cheap, direct proxy for how thin a strike's book usually is (far
    # strikes trade less), so this is a quick visual cross-check against
    # the liquidity story the net-profitability chart tells directly.
    if all_instances:
        by_money: dict[str, int] = defaultdict(int)
        for v in all_instances:
            by_money[moneyness_bucket(v["strike_paise"], v["spot_paise"])] += 1
        bucket_order = ["near_atm (<2%)", "mid (2-5%)", "far (>5%)"]
        values = [by_money.get(b, 0) for b in bucket_order]

        fig, ax = plt.subplots(figsize=(7, 4))
        ax.bar(bucket_order, values, color="#6B675F")
        ax.set_ylabel("violation instances")
        ax.set_title("Violations by distance from spot (moneyness)")
        fig.tight_layout()
        fig.savefig(charts_dir / "violations_by_moneyness.png")
        plt.close(fig)

    print(f"\nCharts written to {charts_dir}/")


if __name__ == "__main__":
    run_backtest()
