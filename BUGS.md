# Bug Report Log

**Rule: log a bug the day you fix it, not at the end of the project.** You will not remember root cause, the wrong turns you took, or why the fix works three weeks later — and this log is the single best source of interview material ("tell me about a bug you found" / "walk me through a tricky debugging session"). A vague memory of "I fixed some data issues" is worthless in an interview; a precise root-cause story is not.

Every real entry answers, from memory: what broke, how it was noticed, what the root cause actually was (not just the symptom), and what changed.

---

## How to file an entry

```markdown
### [BUG-N] Short title

- **Phase:** (0–6)
- **Date found:** YYYY-MM-DD
- **Symptom:** What was observed (error, wrong output, silent bad data — be specific, include the actual numbers/message)
- **Root cause:** What was actually wrong, not just where the crash happened
- **Fix:** What changed
- **How it was caught:** unit test / manual inspection / a check disagreeing with intuition / a crash — this matters for the interview story as much as the bug itself
- **Interview angle:** One sentence — why this is worth telling an interviewer
```

Severity/impact tags are optional but useful once a log has >10 entries: `#data-integrity #logic #concurrency #performance #api-integration`

---

## Decision Log

Not every consequential moment in a project is a bug — some are deliberate calls with real tradeoffs worth remembering precisely, for the same reason bugs are: the nuance won't be there in three weeks, and "why did you build it this way" is as common an interview question as "what bug did you find."

### [DEC-6] Streamlit dashboard as a decoupled reader of a new SQLite log, not a thread bolted onto the WS pipeline

- **Phase:** 4
- **Date:** 2026-08-26
- **Context:** Phase 4's last unchecked item was "a simple alert/log output beyond console logging." `realtime_hybrid.py` is an asyncio program; Streamlit's own execution model is a synchronous script that reruns top-to-bottom on every interaction. Bolting the two together directly (running the WS client inside Streamlit, e.g. via a background thread or `asyncio.run` inside the script) was the tempting first idea, but it also means the dashboard process crashing, being restarted, or being closed by the user would take the live data-collection pipeline down with it.
- **Decision:** keep them fully separate processes with a database as the boundary — a producer/consumer split, same shape as the poller-writes / backtest-reads pattern used elsewhere. `realtime_hybrid.py` (unchanged in its own control flow) now also writes every trigger and every violation it finds to a new `violations_db.py` schema (`trigger_events`, `detected_violations`) in the same `data/snapshots.db` file the poller already uses. `src/dashboard/streamlit_app.py` is a pure read-only consumer: it queries that file on a `time.sleep()` + `st.rerun()` timer, nothing more. Neither process needs to know the other is running.
- **A real follow-on finding, not just a clean design on paper:** a reader (dashboard) and a writer (the pipeline) polling the same SQLite file concurrently hits `sqlite3`'s default rollback-journal locking, which can make a reader block or a writer briefly fail with "database is locked" under real concurrent access. Added `PRAGMA journal_mode=WAL` to `db.py`'s `get_connection()` specifically because this project now has a live concurrent reader for the first time — WAL lets readers and a writer proceed without blocking each other, which the default mode doesn't guarantee.
- **How this was actually verified, not just "it ran without a traceback":** ran `realtime_hybrid.py` live for 20 seconds against the open market, confirmed 27 real trigger rows and 360 real violation rows landed in the database (checked directly via `sqlite3`, not inferred from logs), then loaded the dashboard against that same real data using Streamlit's own `streamlit.testing.v1.AppTest` harness — which actually executes the script and exposes any exception — and confirmed it came back clean, with the KPI metrics matching the raw DB and the earlier console log independently. Also exercised the empty-database path (a fresh schema, zero rows) to confirm the "no data yet" branch doesn't itself crash before the pipeline has ever run.
- **A smaller thing caught along the way:** the dashboard's checkbox for auto-refresh initially defaulted to `True`, which made the very first page load block for 5 seconds before showing anything and made the script hang indefinitely under `AppTest` (which waits for a script run to finish, and a `sleep()`+`rerun()` loop never does). Defaulted it to `False` instead — better first-load experience and incidentally what made the script testable at all.
- **Interview angle:** a concrete example of choosing a boundary (a database, not a shared process or a thread) specifically so two components can fail independently — the dashboard going down should never be able to take real-time detection down with it, and that property comes from the architecture, not from careful coding inside a shared process.

#architecture #concurrency #verification

### [DEC-5] The WebSocket feed only streams LTP with this credential -- full order-book depth mode gets no response at all

- **Phase:** 4
- **Date:** 2026-08-26 (first real market-hours test of the WebSocket layer)
- **Context:** Ran the WebSocket client live for the first time with NSE genuinely open. Connected, subscribed successfully (reference table built, subscribe message sent, no rejection) -- but zero messages arrived in a 55-second window despite subscribing to hundreds of actively-trading NIFTY/BANKNIFTY option legs. That absence of *any* message, not just an absence of violations, was the tell that something deeper than "no arbitrage found" was going on.
- **What was tested, systematically, before concluding anything:** (1) confirmed a "market_info" handshake message arrives automatically on connect, unrelated to subscribing -- so an early apparent "response" wasn't actually evidence the subscribe worked; (2) tested `full_d5` mode (the one this project's entire WS architecture was built around, since it's the only mode carrying bid/ask depth) against a single, maximally liquid instrument (the NIFTY index, then a real ATM option) with both binary and text framing, both `sub` and `change_mode` methods, and both the string and the numeric protobuf enum value for the mode -- all consistently produced **zero response**, including a dedicated 45-second exclusive wait to rule out "just unlucky timing"; (3) as a control, tested `ltpc` mode (last-traded-price only, no depth) against the same instruments -- this **worked**, producing real, correctly-decoded protobuf messages within seconds.
- **Conclusion:** `ltpc` mode works; `full_d5` (and by extension, likely `option_greeks` and `full_d30`) does not, with the access token this project uses. Upstox's own docs explicitly note `full_d30` is "Plus subscribers only" -- consistent with `full_d5` also requiring an entitlement this free, read-only token doesn't carry.
- **Follow-up test, same day: ruled out "token type" as the explanation, conclusively.** Built a full OAuth2 authorization-code flow specifically to test whether a full-App token -- as opposed to the read-only Analytics token -- carries broader WS entitlements. It doesn't: the exact same `full_d5` subscribe test against the exact same liquid ATM option, using the newly-issued OAuth2 App token instead, produced **zero response**, identical to before. Both credential types hit the identical wall, which is strong evidence this is an account/subscription-tier restriction rather than anything about which auth path was used to obtain the token.
- **Interview angle:** a strong, honest example of a real-world integration constraint discovered only by testing against a live account with real entitlements, not discoverable from documentation alone -- and a good illustration of *not* papering over it: it would have been easy to quietly fall back to using LTP in the checks and call the WS layer "done," but that would have silently reintroduced the exact mid-price-leakage failure mode this project is built to avoid.
- **Resolved, built and live-tested same day (2026-08-26):** built `src/data/realtime_hybrid.py` — the `ltpc`-as-trigger hybrid. Subscribes to all tracked legs in `ltpc` mode purely as a trigger signal; on any tick, debounces per (underlying, expiry) group (2s window -- liquid strikes can tick many times a second, re-checking on every one would be wasteful and pointless), then fires a REST `/option/chain` call for that group and runs the no-arbitrage checks against the REST snapshot's real bid/ask -- never against the WS tick's LTP itself. 8 unit tests pass, plus a **live 40-second run against the real open market**: connected cleanly, 54 triggers across the 3 tracked (underlying, expiry) groups (18 each -- consistent with the 2s debounce over a ~37s active window), zero errors or reconnects, REST fetch latency 124-707ms, total tick-to-detection consistently under 1 second. Real violations were detected against live data, including some double-digit-rupee edges on BANKNIFTY -- plausibly explained by wide bid/ask spreads on the specific illiquid strikes involved rather than genuine tradeable arbitrage, which is exactly why DEC-3's OI-based liquidity check still matters as a second filter before trusting a "profitable" edge, not just the presence of a violation. This closes DEC-5: full order-book depth is still unavailable, but the hybrid gets a real, tick-to-detection-latency-measured, honestly-priced detection pipeline running end-to-end without it.

#architecture #verification #finance-domain

### [DEC-4] A single global spline can't find IV outliers -- it bends toward them and contaminates their neighbors

- **Phase:** 5
- **Date:** 2026-08-25
- **Context:** Building the Phase 5 IV-curve-deviation check: fit a smooth curve through a chain's (strike, IV) points, flag strikes whose actual IV deviates from the fit. First implementation was a single global `scipy.UnivariateSpline` fit, then comparing each point against its own fitted value.
- **What actually happened, tested before trusting it:** on a synthetic 40-strike chain with one deliberately injected outlier (15 IV points above a smooth baseline), the naive single-fit approach barely flagged the outlier at all — the fitted curve at the outlier's strike landed within ~4 points of the outlier itself. Root cause: a smoothing spline minimizes squared residuals across *every* point simultaneously, so one large residual just drags the whole local curve toward it rather than standing out against it.
- **First fix attempt, also tested and also wrong:** leave-one-out (fit each point's expected value from every *other* point, excluding only itself). This correctly recovered the outlier's true baseline — but its immediate neighbors were now falsely flagged, because the outlier was still present in *their* leave-one-out fits, still dragging the curve near them even though it no longer dragged the curve at its own location.
- **Actual fix:** two passes. Run leave-one-out once to identify candidate outliers, **exclude those from the fitting set entirely** (not just from their own individual comparison), fit one clean curve on what remains, then re-evaluate every original point — including the excluded candidates — against that clean curve. This is a simplified form of iterative outlier rejection (the same idea behind sigma-clipping in other fields): confirmed on the same synthetic case, this isolates exactly the one real outlier with zero false positives at its neighbors, and recovers a fitted value at the outlier's own strike matching the true underlying baseline almost exactly.
- **How it was caught:** didn't accept the first (or second) implementation because it ran without error and produced *some* deviations — built a synthetic case with a known, injected ground truth specifically so "did this actually work" had a checkable answer, rather than eyeballing real data (where the true answer isn't known) and assuming a plausible-looking result was correct.
- **Interview angle:** a concrete, technical example of why testing statistical/ML code needs synthetic cases with a known ground truth, not just "it runs and the numbers look reasonable" — two different plausible-sounding approaches both failed in specific, different, non-obvious ways that only a ground-truth test could reveal, and the failures were about the *shape* of the error (barely-flagged vs. wrongly-flagged-neighbors), not a crash.

#correctness #methodology #verification

### [DEC-3] "Net-positive after transaction costs" is necessary, not sufficient -- liquidity has to be checked separately

- **Phase:** 3
- **Date:** 2026-08-25
- **Context:** Wired real per-leg transaction costs (brokerage + STT + exchange charges + stamp duty + SEBI + IPFT + GST, `src/models/transaction_costs.py`) into the backtest, replacing an earlier flat-brokerage-only lower bound. First real run: 14/22 detected violations stayed net-positive after ALL real costs, scaled to a full lot. That's a much rosier number than an earlier "0/17 after brokerage alone" — worth being suspicious of a result that swings that hard on one change, not just accepting the bigger number as better.
- **What actually happened:** the single largest "net-positive" instance (a BANKNIFTY calendar violation, net ≈₹4,781/lot) had `min_oi=0` on one leg — literally zero open interest, meaning essentially no one has a live position there. The next-largest thin ones showed the same pattern: bigger nominal edge correlating with lower OI — illiquidity producing a nominal violation that isn't real, this time showing up in the *profitability* number, not just the raw edge.
- **Fix, not just an observation:** added open-interest tracking to every violation type (calendar checks were missing it entirely — `min_oi=None` — until this was caught, a real gap fixed alongside the finding, not just noted), and added an explicit, data-driven warning to the backtest's own output: it now counts how many of the "net-positive" instances fall under a low-OI floor and prints the single largest net-positive instance with its OI directly, rather than only printing a clean-sounding top-line ratio.
- **Why this matters beyond one run:** "net of transaction costs" and "actually executable" are two different claims, and conflating them is exactly the kind of overselling this project is built to avoid. A future version of this backtest should filter or clearly segment by liquidity before reporting a profitability ratio as a headline number — for now, the printed warning does that inline rather than silently.
- **Interview angle:** distinguishing "passes a necessary condition" from "is actually true" is a recurring theme worth having a crisp example for — net-of-cost profitability is necessary for a real opportunity but doesn't establish you could actually get filled at that price and size, and this project caught itself making exactly that overclaim on its own first real run of a new feature.

#correctness #finance-domain #verification

### [DEC-2] Put-call parity check produced 44 false-positive violations from an unstated dividend assumption

- **Phase:** 2
- **Date found:** 2026-08-25
- **Symptom:** First live run of the put-call parity check against a real NIFTY chain (105 strikes, 2026-09-01 expiry) flagged **44 of 101** common strikes as parity violations — all in the *same direction* (`synthetic_short_forward_rich`), with sizeable, fairly consistent edges (₹6.56 to ₹31.38). That pattern — pervasive, one-directional, similarly sized — was itself the tell that something was systematically wrong with the check, not that the market was.
- **Root cause:** the original formula computed the theoretical parity value as `S - K*e^(-rT)` using raw spot price and an assumed risk-free rate, implicitly assuming a **zero dividend yield**. NIFTY is a broad equity index and does carry a real (if modest) dividend yield, so the true forward trades meaningfully above what a dividend-free discounting model predicts. Confirmed directly: backing out the market's own implied forward from the ATM strike's mid prices gave 24,397.95, while raw spot was 24,334.55 — a 63-point gap the original formula only captured about half of.
- **Fix:** stopped assuming a dividend yield at all. Added a function that backs the forward out of a single liquid (ATM) strike's own quoted prices via put-call parity rearranged (`F = K + (C-P)*e^(rT)`), and changed the parity check to take that market-implied forward instead of raw spot. This is the standard practitioner approach for exactly this reason — nobody manually estimates an index's dividend yield when the market's own option prices already embed it. Re-running against the same live chain: 44 violations collapsed to 3, small (₹0.19-1.97) and mixed-direction — consistent with ordinary bid-ask noise at somewhat-thin strikes, not a bug.
- **How it was caught:** didn't report the 44 violations as findings just because the code ran without error — the *pattern* (uniform direction, pervasive, consistent magnitude) was recognizable as a model-bias signature rather than scattered real mispricing, which is exactly the kind of result that should trigger suspicion of the tool before trusting its output.
- **Interview angle:** a real, concrete instance of "when your own tool finds something implausibly large or pervasive, the right instinct is to suspect the tool before the market" — and a clean, honest way to explain a nontrivial finance concept (why forward != spot for a dividend-paying index) that came from debugging, not from reciting a textbook fact.

#correctness #finance-domain #verification

### [DEC-1] Used Upstox's Analytics token instead of building the full OAuth2 flow

- **Phase:** 0
- **Date:** 2026-08-25
- **Context:** The original plan assumed Upstox's general OAuth2 authorization-code flow was the only path to an access token — daily-expiring tokens, requiring a redirect-based login exchange and first-class re-authentication logic in any long-running process (the poller, the WebSocket client). This was a reasonable extrapolation from Upstox's general auth docs, but explicitly flagged at the time as unverified.
- **What actually happened:** once KYC was approved, the account dashboard (`account.upstox.com/developer/apps`) turned out to offer a second, simpler path — an "Analytics" access token, generated with zero app registration, explicitly scoped read-only to Market Data + Real-time & Streaming (exactly this project's needs, no more), with no OAuth2 redirect dance at all. Didn't just trust the dashboard's "1-year validity" claim in prose either — decoded the token's own JWT `iat`/`exp` claims directly and confirmed exactly 365 days.
- **Decision:** use the Analytics token as the project's sole auth mechanism. It's a strict simplification: no client ID/secret/redirect URI to manage, no daily re-auth logic needed, and it can't do anything beyond what this project needs anyway (no trading access) — arguably a *better* security posture for a read-only detection project than a token capable of placing trades.
- **Interview angle:** two things worth naming together — first, not over-building for a failure mode (daily token refresh) before confirming it was actually real, once a simpler, verified alternative existed; second, verifying a specific factual claim (token lifetime) against the primary source (the token's own claims) rather than trusting a secondary description of it, even a first-party one (the dashboard's own text) — a response matching a hypothesis isn't confirmation until it's been checked directly.

#decision #verification #security

---

## Anticipated pitfalls by phase (pre-seeded — not real bugs yet)

Not logged bugs until one actually happens — use this as a checklist, convert an item to a real dated entry the moment it bites, delete an item once confirmed avoided by design.

### Phase 0/1 — Data Layer
- ~~OAuth2 token expiry silently breaking a long-running process~~ — *mostly retired by design: DEC-1 switched this project to Upstox's Analytics access token, confirmed 365-day validity by decoding its own JWT claims, not a daily-expiring token. Still worth a basic auth-failure-vs-network-failure distinction for the eventual year-out expiry, but no longer an everyday concern.*
- **Hardcoded contract specs going stale:** lot size, strike interval, and expiry-day conventions for NIFTY/BANKNIFTY have changed by SEBI mandate before and can again — pull these live from Upstox's instruments data rather than trusting a remembered number.
- **Timezone bugs:** store all timestamps in UTC explicitly, never rely on local time being consistent across runs.
- **Paise vs. rupees float precision:** store option prices as integer paise, not floats — rounding errors compound badly especially since the put-call parity check (Phase 2) is an *exact*-equality bound.
- **Poller crash on one bad/illiquid strike:** a single strike with no quotes, a delisted contract, or a malformed response shouldn't take down the whole polling cycle.

### Phase 2 — Core Model
- **Mid-price leakage:** options naturally invite "the price" thinking (a single IV number, a single premium quoted casually) — always use the correct executable bid/ask side, never a midpoint, in any edge calculation.
- **Wrong direction in the vertical spread check:** flipping which side is "stricter" between calls (decreasing in strike) and puts (increasing in strike) — easy to get backwards since they're mirror images of each other.
- **Risk-free rate assumption error masquerading as a real parity violation:** put-call parity needs an r input; if r is wrong, "violations" near the threshold might just be model-assumption error, not real mispricing — document the assumption and its uncertainty explicitly rather than treating the check's output as ground truth.
- **Transaction costs ignored on multi-leg trades:** a butterfly needs 3 separate legs, each paying brokerage + STT + exchange charges — a "violation" that's profitable gross can easily not be profitable net once every leg's real cost is counted.
- **Unequal strike spacing breaking the butterfly check:** the convexity bound assumes K₂-K₁ = K₃-K₂ exactly — silently applying it to unequally-spaced strikes produces a meaningless number, not a smaller violation.

### Phase 3 — Backtest
- **Look-ahead bias:** only use information available at the time being evaluated.
- **Survivorship bias:** don't silently drop expired/delisted contracts from analysis in a way that biases which violations get seen.
- **Assuming violations will be large or long-lived:** NIFTY/BANKNIFTY options are heavily market-made — expect (and don't be surprised by) small, short-lived violations, and report whatever the data actually shows rather than a preconceived expectation either way.

### Phase 4 — Real-Time
- **Race condition across a 3-leg butterfly, worse than a 2-leg check:** more legs means more opportunities for one to have updated while others are stale.
- **Re-authorizing the WS URL on reconnect:** Upstox's authorize-then-connect pattern means every fresh connection needs a fresh authorized URL via REST first, regardless of how long the underlying access token itself is valid for — a generic reconnect-on-drop handler that skips this step will fail even though the Analytics token (DEC-1) isn't the thing that expired.
- **Missed sequence numbers / silent stale book state:** a gap in the delta stream needs detection and a fresh snapshot, not silent continuation on stale data.

### Phase 5 — Stretch Model
- **Overfitting the IV curve fit:** too many spline knots relative to how many strikes are actually liquid on a given day will fit noise, not the real smile/skew shape.
- **Feature leakage in realized-vol calculation:** a rolling realized-volatility window that accidentally includes data from after the prediction timestamp.

---

## Bug Log

### [BUG-5] Realized volatility returned a raw fraction, silently mismatched against IV's percentage-number convention

- **Phase:** 5
- **Date found:** 2026-08-25
- **Symptom:** First live run of the realized-vs-implied volatility comparison printed "Realized volatility: 0.09%" next to "Implied volatility: 8.64%" -- a 96x gap between two numbers that are supposed to be roughly comparable for the same underlying over a similar timeframe. 0.09% annualized volatility for an equity index is not just wrong, it's essentially impossible (that's calmer than a government bond).
- **Root cause:** the realized-volatility function correctly computed the standard deviation of log returns, annualized (`daily_vol * sqrt(252)`) -- standard math convention returns this as a raw decimal fraction (0.0912 meaning 9.12%). But the IV curve's implied-vol field (and Upstox's own IV field) uses a percentage-NUMBER convention (8.99 meaning 8.99%, not 0.0899). The function's output was printed directly next to an IV value without converting between the two scales -- a 100x unit mismatch that produces a plausible-looking small number (0.09) rather than an error, exactly the kind of bug that's easy to miss on read-through.
- **Fix:** multiplied the function's return value by 100 so it matches the rest of the file's percentage-number convention, and documented the convention explicitly in the docstring. Hand-recomputed the real NIFTY numbers independently (`statistics.stdev` of log returns × √252 × 100 = 9.12) to confirm the corrected value was actually right, not just "a bigger number that looks more plausible."
- **How it was caught:** the printed number was implausible on its face for anyone who knows roughly what index volatility looks like (single-digit to low-double-digit percent, not hundredths of a percent) -- the same "does this number make sense against what I already know" instinct as the parity check's 44 false positives and the ~887 violations/cycle bug below, just applied to a domain-knowledge sanity check rather than a pattern in the data itself.
- **Interview angle:** unit/scale mismatches between two values that are "the same kind of thing" (both volatilities) but conventionally represented differently (fraction vs. percentage number) are a classic, recurring bug family in quant code specifically -- worth having this concrete example ready, and worth naming the general habit: know your libraries' and your own functions' unit conventions explicitly, don't assume two "volatility" numbers are on the same scale just because they're both called volatility.

#correctness #finance-domain #verification

### [BUG-4] `except websockets.exceptions.ConnectionClosed` would itself have crashed on a real drop

- **Phase:** 4
- **Date found:** 2026-08-25
- **Symptom:** Building the reconnect-on-drop loop, writing a test that actually simulates a dropped connection (rather than only unit-testing the happy path) immediately failed with `AttributeError: module 'websockets' has no attribute 'exceptions'` — not the exception the code was trying to catch, a *different* error from inside the `except` clause itself trying to resolve `websockets.exceptions.ConnectionClosed`.
- **Root cause:** the code did `import websockets` (bare) at the top of the reconnect loop, then referenced `websockets.exceptions.ConnectionClosed` in the `except` tuple. In this version of the `websockets` library, submodules like `.exceptions` are lazily loaded and are **not** guaranteed accessible as an attribute just because the top-level package was imported — `websockets.connect()` works fine off a bare import (it's a top-level name), but `websockets.exceptions.*` needs its own explicit `import websockets.exceptions`. The code read as correct on inspection.
- **Fix:** changed to `import websockets.exceptions` explicitly.
- **How it was caught:** by writing a test that actually exercises the failure path (mocking the connection handler to raise, the way a real dropped connection would) instead of only testing that the reconnect loop's happy-path logic (backoff math, max-reconnects counting) was correct in isolation. The happy-path tests alone would never have surfaced this — the bug lives entirely inside the exception-handling path, which only executes when something has already gone wrong.
- **Interview angle:** a sharp, concrete example of why testing error-handling code requires actually triggering the error, not just reading the handler and confirming it "looks right" — a bug in an exception handler is uniquely dangerous because it only manifests exactly when you need the handler to work, i.e. during a real incident, which is the worst possible time to discover your error handling doesn't.

#correctness #tooling

### [BUG-3] Poller had no market-hours awareness, silently wrote thousands of duplicate frozen rows

- **Phase:** 1/3
- **Date found:** 2026-08-25
- **Symptom:** Checked what the poller was actually collecting while NSE was closed. Checked one liquid instrument's quote across 25 consecutive poll cycles (~25 minutes) — `bid`, `ask`, `ltp`, `oi`, and `volume` were **byte-for-byte identical every single cycle**. Not "similar," not "small drift" — exactly identical, which is essentially impossible during real trading (even a quiet market ticks the spread occasionally).
- **Root cause:** the poller had no concept of market hours at all — it polled Upstox's REST option chain endpoint every 60s regardless of the actual exchange session, and outside trading hours that endpoint returns the frozen last-known-state rather than erroring or returning nulls. Nothing in the code path would ever have surfaced this on its own; it only became visible because someone asked what the data actually represented and it got checked directly, row by row, rather than assumed correct because rows were still being written without errors.
- **Why this matters beyond wasted disk space:** it actively corrupts the persistence analysis. A violation present in a frozen snapshot would appear to "persist" across every poll cycle until market reopens — hours of apparent persistence that has nothing to do with real market behavior, which the backtest would misread as an unusually durable, exploitable mispricing.
- **Fix:** added a market-hours check backed by Upstox's own live exchange-status endpoint (confirmed live) rather than a hardcoded trading-hours window — prefer the live source over a remembered constant, and it has the added benefit of automatically handling holidays and special sessions a fixed window wouldn't. Only the exchange's own "open" status counts as open; the run loop pauses and rechecks every 5 minutes while closed instead of polling every 60s into dead air. Purged the ~22,260 duplicate rows written before the fix and restarted the poller clean.
- **How it was caught:** didn't happen from code review or a test — it surfaced because a direct question ("what is the poller pulling right now") prompted actually diffing real rows against each other instead of trusting that "the poller is running and writing rows without errors" meant "the poller is collecting meaningful data."
- **Interview angle:** a clean example of the gap between "the system is running" and "the system is doing something useful" — no exception was ever thrown, no test would have caught this without specifically asserting on cross-cycle variation, and the bug was invisible until someone asked what the output actually meant and checked directly rather than trusting the absence of errors.

#data-integrity #verification

### [BUG-2] WS authorize endpoint: docs page had a stale version prefix and wrong field casing

- **Phase:** 4
- **Date found:** 2026-08-25
- **Symptom:** Called Upstox's documented WS-authorize endpoint at `https://api.upstox.com/v2/feed/market-data-feed/authorize` (the version prefix the page's endpoint-path description implied) with a valid Bearer token — got `410 Gone`, not a 401/404. A `410` specifically means "this used to exist and was deliberately retired," which is a different signal than a typo'd URL would give (that would 404).
- **Root cause:** two separate small inaccuracies in the doc page's rendered text, both caught only by testing live rather than trusting the page: (1) the real, working endpoint is under `/v3/`, not `/v2/` — the `/v2/` path is a retired predecessor, not just an undocumented one; (2) the response field is `authorizedRedirectUri` (camelCase), not the `authorized_redirect_uri` (snake_case) the doc's example showed.
- **Fix:** tested both plausible version prefixes directly (`/v3/` and no prefix) rather than guessing once and moving on; `/v3/` returned `200` with the real response shape, confirmed the actual field name from that live response instead of the doc's prose.
- **How it was caught:** a `410` specifically, not a generic failure, was the tell that pushed toward "this version is retired" rather than "there's a typo" — worth knowing the difference between HTTP status codes precisely, since they point to different next steps (410 → find the current version; 404 → re-check the URL construction).
- **Interview angle:** documentation, even official first-party documentation, describes intent at time-of-writing and drifts from the live API without always being updated in lockstep. The fix isn't "don't trust docs," it's "use docs to form a testable hypothesis, then actually test it before building on top of it."

#api-integration #verification

### [BUG-1] A missing (not zero) ask price got coerced to 0, cascading into ~800 false violations per cycle

- **Phase:** 3
- **Date found:** 2026-08-25
- **Symptom:** First run of the backtest script against real accumulated history reported **16,853 violation instances across 19 poll cycles** — roughly 887 per cycle. A direct, live scan run minutes earlier against essentially the same live chain had found *zero* vertical spread violations. That gap (887 vs. 0) was implausible on its face — a real, structural bug signature, not a burst of real market inefficiency.
- **Root cause:** traced to one instrument, a deep, illiquid NIFTY put (`oi=0`, `volume=0`): its real quote was `bid=₹2.05, ask=null` (no one offering to sell at all). The poller's row-building code used `rupees_to_paise(market_data.get("ask_price") or 0)` — Python's `or 0` silently turns `None` into a literal `0`, which then got stored as `ask_paise=0` and written to the DB as if it were a real, absurdly-cheap ask. The vertical-spread check then read that fabricated near-zero ask as this leg "dominating" nearly every other strike, since almost any real bid exceeds zero — cascading one bad row into hundreds of false violations against every other strike in the same expiry. The original filter only skipped a row when *both* bid and ask were zero/missing (`... and ...`); it needed to require *both* sides genuinely present (no real option is ever quoted at exactly ₹0.00 on either side).
- **Fix:** two layers, not one. (1) Root cause: changed the poller's filter to `not bid_price or not ask_price` (skip if *either* side is missing), so a one-sided quote never gets written to the DB again. (2) Defense in depth: the backtest script also now skips any row with `bid_paise <= 0 or ask_paise <= 0` before building quotes — old rows collected before the fix were already sitting in the DB, and a downstream consumer shouldn't have to trust an upstream producer got it right, especially after just being wrong. Cleared the (small, ~20 minutes of) corrupted history and restarted the poller clean rather than trying to salvage or special-case it.
- **How it was caught:** compared the backtest's output against an independent, already-trusted source (the live direct scan run shortly before) rather than accepting the backtest's number in isolation.
- **Interview angle:** a strong, concrete illustration of why `x or default` is dangerous specifically when `0`/`False`/`""` are valid real values that need distinguishing from "missing" — a very common, very easy-to-write Python bug, and the fact that it silently produced a *plausible-looking but wrong* large number (not a crash) is exactly why the sanity-check-against-an-independent-source habit matters more than trusting code that merely runs without error.

#correctness #data-integrity #verification

---

## Interview angle: what this log demonstrates

Every entry above was caught by testing before trusting an output, not by code review alone — a synthetic ground-truth case for the IV outlier bug, an independent cross-check for the false-positive cascade, an actually-triggered failure path for the reconnect handler, a direct row-by-row diff for the frozen-poller bug. The recurring theme worth naming in an interview: a result that runs without error and looks plausible is not the same as a result that's correct, and the habit of checking anyway — against domain knowledge, against an independent source, against a synthetic case with a known answer — is what actually catches the kind of bug that a type checker or a passing test suite won't.
