#!/usr/bin/env python3
"""kalshi_price_monitor.py — alert on big YES-price moves in a Kalshi event.

Polls every market under the FIFA World Cup Final attendance event
(KXWCATTEND-26JUL20) in one request per cycle and alerts whenever any
market's YES ask moves 10+ cents in either direction between cycles.

Usage:
    python3 kalshi_price_monitor.py             # discover markets, then loop
    python3 kalshi_price_monitor.py --discover  # print event markets, exit
    python3 kalshi_price_monitor.py --once      # discovery + one poll, exit

Requires: Python 3.9+, requests. Optional: plyer (desktop notifications).
"""

from __future__ import annotations

import argparse
import csv
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG = {
    "EVENT_TICKER": "KXWCATTEND-26JUL20",

    "MOVE_ALERT_CENTS": 10,     # single-cycle |Δ yes_ask| that triggers an alert
    "MOVE_COOLDOWN_SECS": 120,  # min seconds between alerts per market
    "STALE_GAP_SECS": 120,      # skip the check if the prior sample is older

    "POLL_SECS": 30,
    "REQUEST_TIMEOUT": 10,
    "BACKOFF_SECS": [30, 60, 120],  # error backoff ladder; last value repeats

    # Alert channels. Discord is used when the webhook URL is non-empty;
    # otherwise a local desktop notification (plyer / osascript / notify-send)
    # plus a sound is attempted.
    "DISCORD_WEBHOOK_URL": "",

    "CSV_PATH": os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "kalshi_price_history.csv"),
}

API_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"


# ---------------------------------------------------------------------------
# API access
# ---------------------------------------------------------------------------

def fetch_markets(session: requests.Session) -> list[dict]:
    """One request for every market in the event. Raises on any failure."""
    resp = session.get(
        API_URL,
        params={"event_ticker": CONFIG["EVENT_TICKER"], "limit": 200},
        timeout=CONFIG["REQUEST_TIMEOUT"],
    )
    resp.raise_for_status()
    markets = resp.json().get("markets")
    if not isinstance(markets, list) or not markets:
        raise ValueError("API returned no markets for event "
                         f"{CONFIG['EVENT_TICKER']}")
    return markets


def price_cents(market: dict, field: str) -> int | None:
    """Read a price in cents, accepting both API shapes.

    Kalshi has served integer-cent fields (yes_ask: 17) and, more recently,
    dollar-string fields (yes_ask_dollars: "0.1700") depending on endpoint
    version. Prefer the integer form, fall back to dollars.
    """
    v = market.get(field)
    if isinstance(v, (int, float)):
        return round(v)
    d = market.get(f"{field}_dollars")
    if d is not None:
        try:
            return round(float(d) * 100)
        except (TypeError, ValueError):
            return None
    return None


def market_label(market: dict) -> str:
    """The person name Kalshi shows for this market (best available field)."""
    return (market.get("yes_sub_title") or market.get("subtitle")
            or market.get("title") or market.get("ticker") or "")


def print_discovery(markets: list[dict]) -> None:
    print(f"\nEvent {CONFIG['EVENT_TICKER']}: {len(markets)} markets")
    for m in sorted(markets, key=market_label):
        print(f"  {m.get('ticker', '?'):32s} {market_label(m)}")


# ---------------------------------------------------------------------------
# Alert channels
# ---------------------------------------------------------------------------

def send_discord(text: str) -> bool:
    url = CONFIG["DISCORD_WEBHOOK_URL"] or os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        return False
    try:
        requests.post(url, json={"content": text},
                      timeout=CONFIG["REQUEST_TIMEOUT"]).raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"    (Discord alert failed: {exc})", file=sys.stderr)
        return False


def send_desktop(title: str, body: str) -> None:
    try:
        from plyer import notification
        notification.notify(title=title, message=body, timeout=10)
        return
    except Exception:
        pass  # plyer missing or no notification backend — try OS tools
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{body}" with title "{title}" '
                 'sound name "Sosumi"'],
                timeout=5, check=False)
        elif system == "Linux" and shutil.which("notify-send"):
            subprocess.run(["notify-send", "-u", "critical", title, body],
                           timeout=5, check=False)
    except Exception as exc:
        print(f"    (desktop notification failed: {exc})", file=sys.stderr)


def play_sound() -> None:
    print("\a", end="", flush=True)  # terminal bell, works everywhere
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["afplay", "/System/Library/Sounds/Sosumi.aiff"])
        elif system == "Linux" and shutil.which("paplay"):
            subprocess.Popen(
                ["paplay", "/usr/share/sounds/freedesktop/stereo/bell.oga"])
    except Exception:
        pass


def alert(name: str, ticker: str, bid, ask, last, delta: int) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = (f"🚨 [MOVEMENT] {ts}  {name} ({ticker})  "
            f"bid={bid}¢ ask={ask}¢ last={last}¢  Δ{delta:+d}¢")
    print("\n" + "=" * len(line))
    print(line)
    print("=" * len(line) + "\n")
    if not send_discord(line):
        send_desktop(f"Kalshi move: {name} {delta:+d}¢",
                     f"bid {bid}¢ / ask {ask}¢ / last {last}¢")
        play_sound()


# ---------------------------------------------------------------------------
# CSV history
# ---------------------------------------------------------------------------

def log_csv(rows: list[list]) -> None:
    path = CONFIG["CSV_PATH"]
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    try:
        with open(path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(["timestamp", "ticker", "name",
                                 "yes_bid", "yes_ask", "last_price"])
            writer.writerows(rows)
    except OSError as exc:
        print(f"    (CSV write failed: {exc})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Monitor loop
# ---------------------------------------------------------------------------

class MarketState:
    """Per-market alert bookkeeping."""

    def __init__(self) -> None:
        self.prev_ask: int | None = None
        self.prev_time: float = 0.0
        self.last_move_alert: float = 0.0


def run_cycle(session: requests.Session, states: dict[str, MarketState]) -> None:
    """One poll: fetch all event markets, check moves, log CSV, print status."""
    markets = fetch_markets(session)
    now = time.time()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    csv_rows = []
    status_bits = []

    for market in sorted(markets, key=market_label):
        ticker = market.get("ticker")
        if not ticker:
            continue
        name = market_label(market)
        state = states.setdefault(ticker, MarketState())

        bid = price_cents(market, "yes_bid")
        ask = price_cents(market, "yes_ask")
        last = price_cents(market, "last_price")
        csv_rows.append([ts, ticker, name, bid, ask, last])

        if ask is None:
            status_bits.append(f"{name}: ?")
            continue
        status_bits.append(f"{name}: {ask}¢")

        # Single-cycle delta, skipping the first sample and stale gaps
        if (state.prev_ask is not None
                and now - state.prev_time <= CONFIG["STALE_GAP_SECS"]):
            delta = ask - state.prev_ask
            if (abs(delta) >= CONFIG["MOVE_ALERT_CENTS"]
                    and now - state.last_move_alert >= CONFIG["MOVE_COOLDOWN_SECS"]):
                alert(name, ticker, bid, ask, last, delta)
                state.last_move_alert = now

        state.prev_ask = ask
        state.prev_time = now

    log_csv(csv_rows)
    print(f"{ts} | " + "  ".join(status_bits), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Alert on 10c+ YES-price moves in a Kalshi event's markets.")
    parser.add_argument("--discover", action="store_true",
                        help="print all event markets, then exit")
    parser.add_argument("--once", action="store_true",
                        help="run discovery plus one poll cycle, then exit")
    args = parser.parse_args()

    session = requests.Session()
    session.headers["User-Agent"] = "kalshi-price-monitor/1.0"

    try:
        markets = fetch_markets(session)
    except (requests.RequestException, ValueError) as exc:
        print(f"FATAL: initial market fetch failed: {exc}", file=sys.stderr)
        return 1

    print_discovery(markets)
    if args.discover:
        return 0

    channel = ("Discord webhook"
               if (CONFIG["DISCORD_WEBHOOK_URL"]
                   or os.environ.get("DISCORD_WEBHOOK_URL"))
               else "desktop notification + sound")
    print(f"\nAlert channel: {channel}")
    print(f"Alert on:      {CONFIG['MOVE_ALERT_CENTS']}¢+ single-cycle moves "
          f"in yes_ask, either direction")
    print(f"CSV history:   {CONFIG['CSV_PATH']}")
    print(f"Polling every {CONFIG['POLL_SECS']}s. Ctrl-C to stop.\n")

    states: dict[str, MarketState] = {}
    backoff_idx = 0
    while True:
        try:
            run_cycle(session, states)
            backoff_idx = 0
            if args.once:
                return 0
            sleep_secs = CONFIG["POLL_SECS"]
        except (requests.RequestException, ValueError, KeyError) as exc:
            ladder = CONFIG["BACKOFF_SECS"]
            sleep_secs = ladder[min(backoff_idx, len(ladder) - 1)]
            backoff_idx += 1
            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
                  f"API error: {exc} — backing off {sleep_secs}s",
                  file=sys.stderr, flush=True)
            if args.once:
                return 1
        try:
            time.sleep(sleep_secs)
        except KeyboardInterrupt:
            print("\nStopping.")
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopping.")
        sys.exit(0)
