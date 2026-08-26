"""Real NSE F&O transaction cost model (Phase 2, FOUNDATIONS.md S8).

Rate schedule confirmed live against upstox.com/brokerage-charges/
(2026-08-25) -- these are NOT permanent constants. STT stepped up from 0.10%
to 0.15% on 2026-04-01 and exchange transaction charges stepped up from
0.03503% to 0.03553% on 2026-03-01; today's date is already past both
changes, so the rates below are the CURRENTLY effective ones, not the older
ones you'd find in an out-of-date source. Re-verify before trusting these in
a future session -- same discipline as not hardcoding NIFTY's lot size
(FOUNDATIONS.md S3), and rates like this are exactly what changes by
government/exchange notification without warning.

Lot sizes confirmed live 2026-08-25: NIFTY 65, BANKNIFTY 30 (also not
permanent -- SEBI has revised these before).
"""
from __future__ import annotations

BROKERAGE_PAISE_PER_ORDER = 2000  # flat Rs 20/order, regardless of quantity

STT_RATE_SELL_SIDE = 0.0015           # 0.15% of premium, sell side only (effective 2026-04-01)
EXCHANGE_TXN_CHARGE_RATE = 0.0003553  # 0.03553% of premium, both sides (effective 2026-03-01)
STAMP_DUTY_RATE_BUY_SIDE = 0.00003    # 0.003% of premium, buy side only
SEBI_CHARGE_RATE = 0.0000001          # Rs 10/crore of turnover, both sides
IPFT_CHARGE_RATE = 0.000005           # Rs 0.50/lakh of premium value, both sides
GST_RATE = 0.18                       # on (brokerage + exchange txn charges + IPFT)

LOT_SIZES = {"NIFTY": 65, "BANKNIFTY": 30}


def leg_cost_paise(premium_paise_per_unit: int, side: str, lot_size: int) -> int:
    """Total real cost (brokerage + all statutory/exchange charges) to open
    ONE leg of ONE lot. side: 'buy' or 'sell' -- STT only applies to sells,
    stamp duty only to buys, everything else applies both ways.
    """
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")

    turnover_paise = premium_paise_per_unit * lot_size

    stt = round(turnover_paise * STT_RATE_SELL_SIDE) if side == "sell" else 0
    exchange_txn = round(turnover_paise * EXCHANGE_TXN_CHARGE_RATE)
    stamp_duty = round(turnover_paise * STAMP_DUTY_RATE_BUY_SIDE) if side == "buy" else 0
    sebi_charge = round(turnover_paise * SEBI_CHARGE_RATE)
    ipft = round(turnover_paise * IPFT_CHARGE_RATE)
    gst = round((BROKERAGE_PAISE_PER_ORDER + exchange_txn + ipft) * GST_RATE)

    return BROKERAGE_PAISE_PER_ORDER + stt + exchange_txn + stamp_duty + sebi_charge + ipft + gst


def trade_cost_paise(legs: list[tuple[str, int]], lot_size: int) -> int:
    """legs: [(side, premium_paise_per_unit), ...] -- one entry per leg,
    repeated if a leg trades multiple units (e.g. the butterfly's middle
    strike, sold twice, should appear twice)."""
    return sum(leg_cost_paise(premium, side, lot_size) for side, premium in legs)
