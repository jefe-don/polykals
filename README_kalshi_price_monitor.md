# kalshi_price_monitor

Polls Kalshi's public market-data API (no auth) every 30 seconds for the
**FIFA World Cup Final: Attendance** event (`KXWCATTEND-26JUL20`) and watches
the YES ask price for: Timothée Chalamet, Victoria Beckham, David Beckham,
Tom Cruise, Tom Brady, Travis Scott, and Drake. All markets are fetched in a
single request per cycle.

## Alerts

- **THRESHOLD** — `yes_ask` drops below 60¢ (per-person threshold in the
  `CONFIG` dict at the top of the file). Fires once, then stays quiet until
  the price re-arms above threshold + 3¢ (hysteresis).
- **MOVEMENT** — any single-cycle move of 10+¢ in `yes_ask`, either direction
  (`MOVE_ALERT_CENTS`). Skipped on the first cycle and after error gaps longer
  than 2 minutes; 2-minute cooldown per market.

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
# Print all event markets and the watch-list ticker matches, then exit
python3 kalshi_price_monitor.py --discover

# Discovery + one poll cycle (verifies prices parse), then exit
python3 kalshi_price_monitor.py --once

# Run the monitor loop (Ctrl-C to stop)
python3 kalshi_price_monitor.py
```

Startup fails loudly (listing unmatched names and all available markets) if
any watched name can't be fuzzy-matched to a market. API errors never crash
the loop — it backs off 30s → 60s → 120s and resumes on success.
