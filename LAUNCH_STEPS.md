# Launch Steps

Plain-language guide to starting, stopping, and checking on this project. Just copy-paste the commands — you don't need to understand everything about them to use them.

**Two quick terms used everywhere below:**
- **The poller** = the background program that fetches live option prices every 60 seconds and saves them. This is the thing that "collects the data."
- **tmux** = a tool that lets a program keep running in the background, even after you close the terminal window. Without it, closing your terminal would kill the poller. (It does *not* survive turning the whole computer off — see below.)

All commands below assume you start from the project folder:
```bash
cd /home/nukebyt/Desktop/projects/system_design/kalshi_mispricing_arbitrage
```

---

## 1. Turned your PC back on? Start the poller like this

**Step 1 — check if it's already running** (right after turning the PC on, it won't be — but it's a good habit to check first so you don't accidentally start two):
```bash
tmux list-sessions
```
- If you see `options-poller` in the list → it's already running, you're done, skip to section 3.
- If it says `no server running`, or you don't see `options-poller` → continue to step 2.

**Step 2 — start it:**
```bash
VENV_PY="$(pwd)/venv/bin/python"
tmux new-session -d -s options-poller "cd $(pwd)/src/data && $VENV_PY poll.py --interval 60 2>&1 | tee -a $(pwd)/data/poll.log"
```
(This just says: "run the poller in the background, and also save everything it prints to a log file.")

**Step 3 — check it started okay:**
```bash
tmux list-sessions
tail -5 data/poll.log
```
You want to see one of these two messages — both are fine:
- `Logged N snapshot rows` — the market is open and it's collecting data
- `Market closed -- pausing polling until it reopens` — the market is closed and it's waiting

Anything that looks like an error message (the word "Traceback", for example) is **not** fine — if you see that, something's wrong and worth asking about.

---

## 2. You don't need to worry about market hours

You can start the poller any time of day — it checks by itself whether the market is open, and just waits (checking again every 5 minutes) until it is. It won't save bad data outside trading hours.

The only thing that matters: **the PC needs to stay on and awake.** If it goes to sleep, or the laptop lid closes, the poller pauses the same way it would if you shut the computer down.

---

## 3. Checking on it later

See the last few things it logged, without interrupting it:
```bash
tail -20 data/poll.log
```

Or watch it live:
```bash
tmux attach -t options-poller
```
To stop watching **without stopping the poller itself**, press `Ctrl+b`, let go, then press `d`.

Want a quick number check — how much data has it collected?
```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('data/snapshots.db')
print('rows:', conn.execute('SELECT COUNT(*) FROM option_snapshots').fetchone()[0])
print('distinct poll cycles:', conn.execute('SELECT COUNT(DISTINCT timestamp_utc) FROM option_snapshots').fetchone()[0])
"
```

---

## 4. Stopping it on purpose

```bash
tmux kill-session -t options-poller
```
You'd only do this if you want to free up your computer for something else while staying on, or before changing the poller's code. If you just shut the PC down without doing this first, nothing bad happens — same result either way.

---

## 5. What happens if you turn the PC off overnight

Nothing breaks. When you turn it back on:
1. Nothing restarts by itself — you'll need to do section 1 above again.
2. The data you already collected is safe (it's just a file on disk).
3. There will be a gap in the data for the time the PC was off — that's expected and fine, not an error.
4. You don't need to log in again or do anything about tokens/passwords — see section 6 below for when that actually becomes relevant.

---

## 6. If the connection to Upstox (the data provider) stops working

This project uses a login key that's valid for about a year (issued 2026-08-25). If it stops working before then, get a new one:
1. Go to `account.upstox.com/developer/apps`
2. Click the "Analytics" tab → "Generate Token"
3. Copy the new key into the `.env` file in this project (open it in a text editor, replace the `UPSTOX_ACCESS_TOKEN` value)

Don't paste the key into a chat message — treat it like a password.

---

## 7. Other one-off commands (not the poller — just single checks)

First, in a new terminal, always run this once:
```bash
source venv/bin/activate
```

| What it does | Command |
|---|---|
| Grab one live snapshot of option prices right now | `cd src/data && python fetch_option_chain.py` |
| Run the pricing-consistency checks against live data | `cd src/models && python consistency.py` |
| Run the analysis over everything collected so far | `cd src/backtest && python analyze_violations.py` |
| Try a live real-time connection | `cd src/data && python realtime_hybrid.py` — only does anything while the market is open |
| View the dashboard in your browser | `streamlit run src/dashboard/streamlit_app.py` |
| Run all the automated tests | `python -m pytest tests/ -v` (run from the project's main folder) |

---

## 8. Setting this up on a brand new computer (first time only)

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```
Then open `.env` in a text editor and fill in `UPSTOX_ACCESS_TOKEN` yourself.
