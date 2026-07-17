# kalshi_price_monitor

Polls Kalshi's public market-data API (no auth) every 30 seconds for **every
market** under the **FIFA World Cup Final: Attendance** event
(`KXWCATTEND-26JUL20`) — one batched request per cycle — and alerts whenever
any person's YES ask price moves **10¢ or more in a single cycle**, in either
direction (`MOVE_ALERT_CENTS` in the `CONFIG` dict at the top of the file).

The movement check is skipped on the first cycle and after error gaps longer
than 2 minutes (no stale comparisons), and each market has a 2-minute alert
cooldown so a sustained slide doesn't ping every 30 seconds. Markets added to
the event mid-run are picked up automatically.

Alerts print a loud console line and go to a **Discord webhook** if
`DISCORD_WEBHOOK_URL` is set (in `CONFIG` or as an env var); otherwise a
local desktop notification + sound is used (plyer, falling back to
`osascript` on macOS / `notify-send` on Linux).

Every poll cycle is appended to `kalshi_price_history.csv`
(timestamp, ticker, name, yes_bid, yes_ask, last_price).

## Setup

```bash
pip install -r requirements.txt
```

(`plyer` is optional — only needed for desktop notifications.)

## Usage

```bash
# Print all markets in the event, then exit
python3 kalshi_price_monitor.py --discover

# Discovery + one poll cycle (verifies prices parse), then exit
python3 kalshi_price_monitor.py --once

# Run the monitor loop (Ctrl-C to stop)
python3 kalshi_price_monitor.py
```

API errors never crash the loop — it backs off 30s → 60s → 120s and resumes
on success.
