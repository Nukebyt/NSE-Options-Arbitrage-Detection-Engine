"""Phase 5: implied volatility curve fitting -- an independent, stat-arb
(NOT structural) fair-value signal (FOUNDATIONS.md S28-33).

Fits a smooth curve through observed (strike, IV) points for one expiry,
then flags strikes whose actual IV deviates meaningfully from the fitted
curve's value at that strike. This is fundamentally different in kind from
every Phase 2 check: those have a genuine no-arbitrage replication argument
behind them (FOUNDATIONS.md S9-S18); this is a VIEW -- "this strike's IV
looks out of line with its neighbors' smooth shape" -- not a guaranteed
mispricing (FOUNDATIONS.md S10's stat-arb/structural-arb distinction, S30).
A flagged deviation is a signal worth investigating, very often explained by
stale or thin liquidity at that specific strike (same caveat as the
calendar check, FOUNDATIONS.md S17) rather than a real, executable edge.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.interpolate import UnivariateSpline


@dataclass(frozen=True)
class IVCurvePoint:
    strike_paise: int
    instrument_key: str
    option_type: str
    observed_iv: float


@dataclass(frozen=True)
class IVDeviation:
    strike_paise: int
    instrument_key: str
    option_type: str
    observed_iv: float
    fitted_iv: float
    deviation: float  # observed - fitted, in IV percentage points


def fit_iv_curve(points: list[IVCurvePoint], smoothing: float | None = None) -> UnivariateSpline:
    """Fits a cubic smoothing spline through (strike, IV) points. Requires
    at least 4 DISTINCT strikes (a cubic spline needs >= k+1 = 4 points) --
    raises ValueError rather than silently returning a degenerate fit on
    too little data, the same "don't trust a fit you can't actually
    support" instinct as FOUNDATIONS.md S26's overfitting warning.

    smoothing=None lets scipy pick automatically; pass a small positive
    number to force a smoother (less wiggly) curve if the automatic choice
    overfits the observed points -- worth checking visually before trusting
    on real data, not just accepting scipy's default.
    """
    sorted_points = sorted(points, key=lambda p: p.strike_paise)
    seen: set[int] = set()
    deduped = []
    for p in sorted_points:
        if p.strike_paise not in seen:
            deduped.append(p)
            seen.add(p.strike_paise)

    if len(deduped) < 4:
        raise ValueError(f"need at least 4 distinct strikes to fit a stable IV curve, got {len(deduped)}")

    strikes = np.array([p.strike_paise for p in deduped], dtype=float)
    ivs = np.array([p.observed_iv for p in deduped], dtype=float)
    return UnivariateSpline(strikes, ivs, k=3, s=smoothing)


def _leave_one_out_deviations(points: list[IVCurvePoint], threshold: float, smoothing: float | None) -> list[IVDeviation]:
    deviations = []
    for i, target in enumerate(points):
        others = points[:i] + points[i + 1:]
        spline = fit_iv_curve(others, smoothing=smoothing)
        fitted = float(spline(target.strike_paise))
        diff = target.observed_iv - fitted
        if abs(diff) >= threshold:
            deviations.append(IVDeviation(target.strike_paise, target.instrument_key, target.option_type, target.observed_iv, fitted, diff))
    return deviations


def find_iv_deviations(points: list[IVCurvePoint], threshold: float = 2.0, smoothing: float | None = None) -> list[IVDeviation]:
    """threshold: minimum |observed - fitted| IV difference, in IV
    percentage points (e.g. 2.0 = a 2-point gap, like IV=18 vs a fitted 16),
    to flag as worth reporting.

    Two passes, not one -- tried the simpler versions first and both failed
    on a realistic synthetic test (a 40-strike chain with one injected
    15-point IV outlier) before landing here:

    1. A single global fit, comparing every point to itself: the outlier
       dragged the curve toward itself (a smoothing spline minimizes
       squared residuals across ALL points), so it was barely flagged at
       all, AND it warped the fitted value at its neighboring strikes too.
    2. Leave-one-out (each point compared against a fit built from every
       OTHER point): correctly recovered the outlier's true baseline (fitted
       exactly at the "clean" level), but its immediate neighbors were STILL
       flagged as false positives, because the outlier remains present in
       *their* leave-one-out fits even though it's absent from its own.

    The actual fix: run leave-one-out once to find candidate outliers, drop
    those from the fitting set entirely, fit one clean curve on what's left,
    then re-evaluate every original point against that clean curve. This is
    a simplified form of iterative outlier rejection (the same idea behind
    sigma-clipping) -- excluding a confirmed outlier from the reference
    curve, not just from its own leave-one-out comparison, is what stops it
    from contaminating its neighbors' expected values too.
    """
    if len(points) < 5:
        raise ValueError("need at least 5 points for leave-one-out (4 to fit + 1 to test)")

    candidates = _leave_one_out_deviations(points, threshold, smoothing)
    candidate_keys = {d.instrument_key for d in candidates}
    clean_points = [p for p in points if p.instrument_key not in candidate_keys]

    if len(clean_points) < 4:
        # Too many candidates to safely exclude and still fit a curve --
        # fall back to the leave-one-out result rather than fail outright.
        return candidates

    clean_spline = fit_iv_curve(clean_points, smoothing=smoothing)
    deviations = []
    for target in points:
        fitted = float(clean_spline(target.strike_paise))
        diff = target.observed_iv - fitted
        if abs(diff) >= threshold:
            deviations.append(IVDeviation(target.strike_paise, target.instrument_key, target.option_type, target.observed_iv, fitted, diff))
    return deviations


def realized_volatility(daily_closes: list[float], trading_days_per_year: int = 252) -> float:
    """Annualized realized volatility from a list of daily closing prices,
    oldest first, via the standard deviation of log returns (FOUNDATIONS.md
    S31). Requires at least 2 closes (1 return); more is more reliable --
    treat a result from a short window with the same small-sample caution
    as everywhere else in this project (FOUNDATIONS.md S23).

    Returns a PERCENTAGE NUMBER (e.g. 9.12 meaning 9.12%), matching
    IVCurvePoint.observed_iv's convention (Upstox's own IV field is quoted
    the same way, e.g. iv=8.99), NOT a raw decimal fraction (0.0912) --
    caught for real: an earlier version returned the raw fraction, and
    printing it directly next to an IV value produced "0.09%" against a real
    NIFTY realized vol that manual recomputation confirmed was actually
    9.12%, a silent 100x scale mismatch nobody would catch just by reading
    the code, only by noticing the printed number was implausible.
    """
    if len(daily_closes) < 2:
        raise ValueError("need at least 2 daily closes to compute a single log return")

    log_returns = [math.log(daily_closes[i] / daily_closes[i - 1]) for i in range(1, len(daily_closes))]
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / max(len(log_returns) - 1, 1)
    daily_vol = math.sqrt(variance)
    return daily_vol * math.sqrt(trading_days_per_year) * 100


if __name__ == "__main__":
    import sys
    from datetime import date
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data"))
    from fetch_option_chain import NIFTY, get_historical_daily_closes, get_option_chain, list_expiries

    today = date.today().isoformat()
    expiry = next(e for e in list_expiries(NIFTY) if e > today)
    print(f"Fitting IV curve for NIFTY {expiry}...")

    chain = get_option_chain(NIFTY, expiry)
    points = []
    for row in chain:
        strike_paise = round(row["strike_price"] * 100)
        for option_type, leg_key in (("CE", "call_options"), ("PE", "put_options")):
            leg = row.get(leg_key) or {}
            md = leg.get("market_data") or {}
            greeks = leg.get("option_greeks") or {}
            iv = greeks.get("iv")
            # iv=0.0 means the Greeks solver didn't converge (deep ITM/OTM,
            # thin legs) -- FOUNDATIONS.md S28 / ROADMAP.md Phase 1's nullable-iv
            # decision. A zero here is a missing value, not a real 0% IV.
            if md.get("bid_price") and md.get("ask_price") and iv:
                points.append(IVCurvePoint(strike_paise, leg["instrument_key"], option_type, iv))

    print(f"{len(points)} usable (strike, IV) points across calls and puts")

    for label in ("CE", "PE"):
        subset = [p for p in points if p.option_type == label]
        if len(subset) < 4:
            print(f"\n{label}: only {len(subset)} usable points, skipping (need >=4)")
            continue
        deviations = find_iv_deviations(subset, threshold=2.0)
        print(f"\n{label}: {len(subset)} points, {len(deviations)} deviations >=2 IV points from the fitted curve")
        for d in sorted(deviations, key=lambda d: -abs(d.deviation))[:5]:
            print(f"  strike={d.strike_paise / 100:<10} observed_iv={d.observed_iv:<6.2f} fitted_iv={d.fitted_iv:<6.2f} deviation={d.deviation:+.2f}")

    print("\n--- Realized vs. implied volatility (FOUNDATIONS.md S31) ---")
    from datetime import timedelta

    lookback_start = (date.today() - timedelta(days=35)).isoformat()
    closes = get_historical_daily_closes(NIFTY, lookback_start, today)
    if len(closes) < 5:
        print(f"Only {len(closes)} daily closes available, skipping realized-vol comparison (need >=5 for a stable estimate).")
    else:
        close_values = [c for _, c in closes]
        realized = realized_volatility(close_values)
        atm_ivs = [p.observed_iv for p in points if abs(p.strike_paise - round(chain[0]["underlying_spot_price"] * 100)) < 5000]
        implied_atm = sum(atm_ivs) / len(atm_ivs) if atm_ivs else float("nan")
        print(f"Realized volatility ({len(closes)} trading days, {closes[0][0][:10]} to {closes[-1][0][:10]}): {realized:.2f}%")
        print(f"Implied volatility (ATM average, this expiry): {implied_atm:.2f}%")
        print(f"Gap (implied - realized): {implied_atm - realized:+.2f} points")
        print("NOTE (FOUNDATIONS.md S10): this is a VIEW, not a structural arbitrage signal -- realized vol")
        print("running below implied is a classic vol-selling thesis, not a guaranteed edge. Also note realized")
        print(f"vol here uses only {len(closes)} trading days -- a short window (FOUNDATIONS.md S23 small-sample caution).")
