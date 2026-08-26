"""Phase 2: no-arbitrage checks for NSE index options.

See FOUNDATIONS.md Part III for full derivations and worked examples. All
prices/strikes here are integer paise (FOUNDATIONS.md S35).

Note on the retired Kalshi-phase check_monotonic(): FOUNDATIONS.md originally
claimed it would transfer to option vertical spreads with zero code changes.
That turned out to be wrong on closer derivation, not just a stylistic
rewrite -- see BUGS.md DEC-3. The abstract principle (a dominated payoff
can't be priced above the payoff that dominates it) is genuinely shared; the
concrete formula isn't, because a Kalshi YES/NO pair always pays out >=100c
(a fixed floor), while an option vertical spread's payoff floor is 0 (not a
fixed positive number). check_vertical_spread() below is a new, independently
derived implementation, not a port.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class OptionQuote:
    instrument_key: str
    option_type: str  # "CE" or "PE"
    bid_paise: int
    ask_paise: int


@dataclass(frozen=True)
class VerticalSpreadViolation:
    dominant_key: str
    dominated_key: str
    option_type: str
    edge_paise: int


@dataclass(frozen=True)
class PutCallParityViolation:
    call_key: str
    put_key: str
    strike_paise: int
    direction: str  # "synthetic_long_forward_cheap" | "synthetic_short_forward_rich"
    edge_paise: int


@dataclass(frozen=True)
class ConvexityViolation:
    option_type: str
    wing_low_key: str
    middle_key: str
    wing_high_key: str
    edge_paise: int


@dataclass(frozen=True)
class CalendarViolation:
    near_expiry: str
    far_expiry: str
    strike_paise: int
    option_type: str
    near_key: str
    far_key: str
    edge_paise: int


def check_vertical_spread(strikes_and_quotes: list[tuple[int, OptionQuote]], option_type: str) -> list[VerticalSpreadViolation]:
    """strikes_and_quotes: [(strike_paise, quote), ...] for one underlying/expiry/option_type.

    Calls: lower strike dominates (its payoff >= a higher strike's in every
    state), so C(K1) >= C(K2) is required for K1 < K2. Puts: higher strike
    dominates instead (FOUNDATIONS.md S12). Violation = the dominated leg's
    bid exceeds the dominant leg's ask -- buying the dominant leg cheap and
    selling the dominated leg rich is a guaranteed non-negative-payoff
    position entered for a net credit (FOUNDATIONS.md S12-13).

    Checks every pair, not just adjacent strikes, for the same reason the
    Kalshi-phase ladder check did: a violation between non-adjacent strikes
    is just as real.
    """
    if option_type not in ("CE", "PE"):
        raise ValueError(f"option_type must be 'CE' or 'PE', got {option_type!r}")

    ordered = sorted(strikes_and_quotes, key=lambda pair: pair[0])
    higher_strike_is_dominant = option_type == "PE"

    violations = []
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            lower_quote = ordered[i][1]
            higher_quote = ordered[j][1]
            dominant, dominated = (higher_quote, lower_quote) if higher_strike_is_dominant else (lower_quote, higher_quote)

            edge = dominated.bid_paise - dominant.ask_paise
            if edge > 0:
                violations.append(
                    VerticalSpreadViolation(
                        dominant_key=dominant.instrument_key,
                        dominated_key=dominated.instrument_key,
                        option_type=option_type,
                        edge_paise=edge,
                    )
                )
    return violations


def implied_forward_paise(atm_call: OptionQuote, atm_put: OptionQuote, atm_strike_paise: int, risk_free_rate: float, years_to_expiry: float) -> float:
    """Back out the market-implied forward from a liquid (ideally ATM) strike's
    mid prices, via put-call parity rearranged: F = K + (C - P)*e^(rT).

    Deliberately NOT using spot * e^((r-q)T) with an assumed dividend yield q
    -- an early version of this module did exactly that (assuming q=0) and it
    produced 44 same-direction "violations" across one real NIFTY chain,
    which is the signature of a systematic model bias, not real mispricing
    (see BUGS.md DEC-4). The market's own ATM quotes already embed the true
    dividend/carry effect; backing the forward out from them avoids needing
    to know or guess the dividend yield at all -- the standard practitioner
    approach, not a workaround.
    """
    call_mid = (atm_call.bid_paise + atm_call.ask_paise) / 2
    put_mid = (atm_put.bid_paise + atm_put.ask_paise) / 2
    return atm_strike_paise + (call_mid - put_mid) * math.exp(risk_free_rate * years_to_expiry)


def check_put_call_parity(
    call: OptionQuote,
    put: OptionQuote,
    implied_forward_paise: float,
    strike_paise: int,
    risk_free_rate: float,
    years_to_expiry: float,
) -> PutCallParityViolation | None:
    """Put-call parity (FOUNDATIONS.md S10-11): C - P should equal (F - K)*e^(-rT),
    where F is the market-implied forward (see implied_forward_paise() above),
    not raw spot -- see that function's docstring and BUGS.md DEC-4 for why.

    SCOPE LIMITATION, stated explicitly rather than glossed over: this is an
    internal-consistency check across strikes at the same expiry (does this
    strike's parity agree with the reference strike's?), not a check against
    an independently-observed forward price. Fully capturing this as a
    tradeable arbitrage would still benefit from an independent NIFTY futures
    quote (Upstox's API supports this; not yet integrated -- see ROADMAP.md
    Phase 2).
    """
    theoretical_diff = (implied_forward_paise - strike_paise) * math.exp(-risk_free_rate * years_to_expiry)

    # synthetic long forward: buy call (ask), sell put (bid)
    synthetic_long_cost = call.ask_paise - put.bid_paise
    long_edge = theoretical_diff - synthetic_long_cost
    if long_edge > 0:
        return PutCallParityViolation(call.instrument_key, put.instrument_key, strike_paise, "synthetic_long_forward_cheap", round(long_edge))

    # synthetic short forward: sell call (bid), buy put (ask)
    synthetic_short_proceeds = call.bid_paise - put.ask_paise
    short_edge = synthetic_short_proceeds - theoretical_diff
    if short_edge > 0:
        return PutCallParityViolation(call.instrument_key, put.instrument_key, strike_paise, "synthetic_short_forward_rich", round(short_edge))

    return None


def check_convexity(
    low: OptionQuote,
    mid: OptionQuote,
    high: OptionQuote,
    low_strike_paise: int,
    mid_strike_paise: int,
    high_strike_paise: int,
) -> ConvexityViolation | None:
    """Butterfly convexity bound (FOUNDATIONS.md S14-15). Requires equally
    spaced strikes. Trade: buy 1 low + buy 1 high, sell 2 mid -- payoff is
    always >= 0, so a negative cost is a guaranteed profit.
    """
    if (mid_strike_paise - low_strike_paise) != (high_strike_paise - mid_strike_paise):
        raise ValueError("check_convexity requires equally spaced strikes")
    if not (low.option_type == mid.option_type == high.option_type):
        raise ValueError("check_convexity requires all three legs to be the same option_type")

    cost = low.ask_paise + high.ask_paise - 2 * mid.bid_paise
    edge = -cost
    if edge > 0:
        return ConvexityViolation(low.option_type, low.instrument_key, mid.instrument_key, high.instrument_key, edge)
    return None


def check_calendar_monotonic(
    near_quote: OptionQuote,
    near_expiry: str,
    far_quote: OptionQuote,
    far_expiry: str,
    strike_paise: int,
) -> CalendarViolation | None:
    """Calendar monotonicity (FOUNDATIONS.md S17): same strike, same option
    type, a later-expiry option is generally expected to be worth at least
    as much as an earlier-expiry one -- more time to expiry means more
    optionality value, all else equal.

    IMPORTANT, stated explicitly rather than glossed over: unlike
    check_vertical_spread/check_put_call_parity/check_convexity, this is
    NOT a strict, guaranteed no-arbitrage bound for European options. Those
    three checks all have a genuine riskless-replication argument behind
    them (FOUNDATIONS.md S12/S10/S14) -- this one doesn't: if you buy the
    far-dated leg and sell the near-dated leg and the near leg finishes ITM,
    there's no riskless unwind at that point, you're just left holding a
    naked far-dated option of unknown future value, not a locked profit.
    Treat a flagged result here as a genuine anomaly worth investigating
    (an unusual quote, or a real dividend/corporate-action effect the
    "generally expected" reasoning doesn't account for), not a guaranteed
    risk-free trade the way the other three checks are.
    """
    if near_expiry >= far_expiry:
        raise ValueError("near_expiry must be strictly before far_expiry")
    if near_quote.option_type != far_quote.option_type:
        raise ValueError("both legs must be the same option_type")

    edge = near_quote.bid_paise - far_quote.ask_paise
    if edge > 0:
        return CalendarViolation(
            near_expiry, far_expiry, strike_paise, near_quote.option_type,
            near_quote.instrument_key, far_quote.instrument_key, edge,
        )
    return None


def _build_books(chain: list[dict]) -> tuple[dict[int, "OptionQuote"], dict[int, "OptionQuote"]]:
    calls_by_strike: dict[int, OptionQuote] = {}
    puts_by_strike: dict[int, OptionQuote] = {}
    for row in chain:
        strike_paise = round(row["strike_price"] * 100)
        for option_type, leg_key, store in (("CE", "call_options", calls_by_strike), ("PE", "put_options", puts_by_strike)):
            leg = row.get(leg_key)
            md = (leg or {}).get("market_data") or {}
            if md.get("bid_price") and md.get("ask_price"):
                store[strike_paise] = OptionQuote(
                    instrument_key=leg["instrument_key"],
                    option_type=option_type,
                    bid_paise=round(md["bid_price"] * 100),
                    ask_paise=round(md["ask_price"] * 100),
                )
    return calls_by_strike, puts_by_strike


if __name__ == "__main__":
    import sys
    from datetime import date
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data"))
    from fetch_option_chain import NIFTY, get_option_chain, list_expiries

    RISK_FREE_RATE = 0.065  # approximate current repo-adjacent rate; see FOUNDATIONS.md S2, Phase 2 note

    today = date.today().isoformat()
    future_expiries = [e for e in list_expiries(NIFTY) if e > today]
    expiry = future_expiries[0]
    years_to_expiry = (date.fromisoformat(expiry) - date.fromisoformat(today)).days / 365.0
    print(f"Scanning NIFTY {expiry} ({years_to_expiry:.4f}y to expiry)...")

    chain = get_option_chain(NIFTY, expiry)
    spot_paise = round(chain[0]["underlying_spot_price"] * 100)
    calls_by_strike, puts_by_strike = _build_books(chain)

    total_violations = 0

    for label, book in (("CE", calls_by_strike), ("PE", puts_by_strike)):
        violations = check_vertical_spread(list(book.items()), label)
        for v in violations:
            total_violations += 1
            print(f"  VERTICAL SPREAD VIOLATION [{label}]: {v}")

    common_strikes = sorted(set(calls_by_strike) & set(puts_by_strike))
    atm_strike = min(common_strikes, key=lambda k: abs(k - spot_paise))
    forward_paise = implied_forward_paise(calls_by_strike[atm_strike], puts_by_strike[atm_strike], atm_strike, RISK_FREE_RATE, years_to_expiry)
    print(f"Spot: {spot_paise / 100}, ATM strike: {atm_strike / 100}, implied forward: {forward_paise / 100:.2f}")

    for strike_paise in common_strikes:
        v = check_put_call_parity(calls_by_strike[strike_paise], puts_by_strike[strike_paise], forward_paise, strike_paise, RISK_FREE_RATE, years_to_expiry)
        if v:
            total_violations += 1
            print(f"  PARITY VIOLATION [strike={strike_paise / 100}]: {v}")

    for label, book in (("CE", calls_by_strike), ("PE", puts_by_strike)):
        strikes = sorted(book)
        for i in range(len(strikes) - 2):
            k1, k2, k3 = strikes[i], strikes[i + 1], strikes[i + 2]
            if (k2 - k1) != (k3 - k2):
                continue
            v = check_convexity(book[k1], book[k2], book[k3], k1, k2, k3)
            if v:
                total_violations += 1
                print(f"  CONVEXITY VIOLATION [{label}]: {v}")

    if len(future_expiries) > 1:
        far_expiry = future_expiries[1]
        print(f"\nScanning calendar check against {far_expiry}...")
        far_chain = get_option_chain(NIFTY, far_expiry)
        far_calls, far_puts = _build_books(far_chain)
        for label, near_book, far_book in (("CE", calls_by_strike, far_calls), ("PE", puts_by_strike, far_puts)):
            for strike_paise in sorted(set(near_book) & set(far_book)):
                v = check_calendar_monotonic(near_book[strike_paise], expiry, far_book[strike_paise], far_expiry, strike_paise)
                if v:
                    total_violations += 1
                    print(f"  CALENDAR VIOLATION [{label} strike={strike_paise / 100}]: {v}")
    else:
        print("\nOnly one future expiry available -- skipping calendar check this run.")

    print(f"\n{len(common_strikes)} common strikes checked, {len(chain)} total rows.")
    print(f"Total violations found: {total_violations}")
