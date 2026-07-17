#!/usr/bin/env python3
"""kalshi_price_monitor.py — poll Kalshi YES prices for watched attendance markets.

Watches the FIFA World Cup Final attendance event (KXWCATTEND-26JUL20) and
alerts when a watched person's YES ask price crosses below their threshold,
or moves 10+ cents in a single poll cycle. All watched markets are fetched in
one request per cycle via the event_ticker query param.

Usage:
    python3 kalshi_price_monitor.py             # discover tickers, then loop
    python3 kalshi_price_monitor.py --discover  # print markets + matches, exit
    python3 kalshi_price_monitor.py --once      # discover + one poll cycle, exit

Requires: Python 3.9+, requests. Optional: plyer (desktop notifications).
"""

from __future__ import annotations

import argparse
import csv
import difflib
import os
import platform
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import datetime

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG = {
    "EVENT_TICKER": "KXWCATTEND-26JUL20",

    # People to watch → per-person THRESHOLD alert level in cents.
    # Alert fires once when yes_ask drops below this; re-arms after yes_ask
    # rises back above threshold + REARM_CENTS.
    "WATCH": {
        "Timothée Chalamet": 60,
        "Victoria Beckham": 60,
        "David Beckham": 60,
        "Tom Cruise": 60,
        "Tom Brady": 60,
        "Travis Scott": 60,
        "Drake": 60,
    },
    "REARM_CENTS": 3,           # hysteresis above threshold before re-arming

    "MOVE_ALERT_CENTS": 10,     # single-cycle |Δ yes_ask| that triggers MOVEMENT
    "MOVE_COOLDOWN_SECS": 120,  # min seconds between MOVEMENT alerts per market
    "STALE_GAP_SECS": 120,      # skip MOVEMENT check if prior sample older than this

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
FUZZY_MIN_SCORE = 0.72  # minimum difflib ratio to accept a name↔market match


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


# ---------------------------------------------------------------------------
# Ticker discovery / fuzzy matching
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    """Lowercase, accent-stripped form for fuzzy comparison."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.casefold().split())


def market_label(market: dict) -> str:
    """The person name Kalshi shows for this market (best available field)."""
    return (market.get("yes_sub_title") or market.get("subtitle")
            or market.get("title") or market.get("ticker") or "")


def match_watchlist(markets: list[dict]) -> dict[str, dict]:
    """Map each watched name to exactly one market, or die loudly.

    Scores every (name, market) pair with difflib on normalized strings and
    assigns greedily from the best score down, so near-collisions like
    'Travis Scott' vs the event's 'Travis Kelce' market resolve to the
    exact-match market first.
    """
    pairs = []  # (score, name, ticker)
    by_ticker = {m["ticker"]: m for m in markets if m.get("ticker")}
    for name in CONFIG["WATCH"]:
        n = _norm(name)
        for ticker, market in by_ticker.items():
            label = _norm(market_label(market))
            if not label:
                continue
            score = difflib.SequenceMatcher(None, n, label).ratio()
            if n == label:
                score = 1.0
            elif n in label or label in n:
                score = max(score, 0.9)
            if score >= FUZZY_MIN_SCORE:
                pairs.append((score, name, ticker))

    matched: dict[str, dict] = {}
    used_tickers: set[str] = set()
    for score, name, ticker in sorted(pairs, key=lambda p: -p[0]):
        if name in matched or ticker in used_tickers:
            continue
        matched[name] = by_ticker[ticker]
        used_tickers.add(ticker)

    unmatched = [n for n in CONFIG["WATCH"] if n not in matched]
    if unmatched:
        print("\nFATAL: could not match these watched names to a market:",
              file=sys.stderr)
        for name in unmatched:
            print(f"  - {name}", file=sys.stderr)
        print("\nAll available markets in this event:", file=sys.stderr)
        for m in markets:
            print(f"  {m.get('ticker', '?'):32s} {market_label(m)}",
                  file=sys.stderr)
        sys.exit(1)
    return matched


def print_discovery(markets: list[dict], matched: dict[str, dict]) -> None:
    print(f"\nEvent {CONFIG['EVENT_TICKER']}: {len(markets)} markets")
    for m in markets:
        print(f"  {m.get('ticker', '?'):32s} {market_label(m)}")
    print("\nWatch list matches:")
    for name, m in matched.items():
        print(f"  {name:22s} → {m['ticker']:32s} ({market_label(m)})")


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


def alert(kind: str, name: str, ticker: str, bid, ask, last,
          delta: int | None = None) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    delta_txt = f"  Δ{delta:+d}¢" if delta is not None else ""
    line = (f"🚨 [{kind}] {ts}  {name} ({ticker})  "
            f"bid={bid}¢ ask={ask}¢ last={last}¢{delta_txt}")
    print("\n" + "=" * len(line))
    print(line)
    print("=" * len(line) + "\n")
    if not send_discord(line):
        send_desktop(f"Kalshi {kind}: {name}",
                     f"bid {bid}¢ / ask {ask}¢ / last {last}¢{delta_txt}")
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
        self.threshold_armed = True
        self.last_move_alert: float = 0.0


def run_cycle(session: requests.Session, name_to_ticker: dict[str, str],
              states: dict[str, MarketState]) -> None:
    """One poll: fetch, evaluate alerts, log CSV, print status line."""
    markets = {m["ticker"]: m for m in fetch_markets(session) if m.get("ticker")}
    now = time.time()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    csv_rows = []
    status_bits = []

    for name, ticker in name_to_ticker.items():
        state = states[ticker]
        market = markets.get(ticker)
        if market is None:
            print(f"    WARNING: {ticker} ({name}) missing from API response",
                  file=sys.stderr)
            status_bits.append(f"{name}: ?")
            continue

        bid = price_cents(market, "yes_bid")
        ask = price_cents(market, "yes_ask")
        last = price_cents(market, "last_price")
        csv_rows.append([ts, ticker, name, bid, ask, last])

        if ask is None:
            status_bits.append(f"{name}: ?")
            continue
        status_bits.append(f"{name}: {ask}¢")

        # THRESHOLD: fire once below threshold; re-arm above threshold + hysteresis
        threshold = CONFIG["WATCH"][name]
        if state.threshold_armed and ask < threshold:
            alert("THRESHOLD", name, ticker, bid, ask, last)
            state.threshold_armed = False
        elif not state.threshold_armed and ask > threshold + CONFIG["REARM_CENTS"]:
            state.threshold_armed = True

        # MOVEMENT: single-cycle delta, skipping first sample and stale gaps
        if (state.prev_ask is not None
                and now - state.prev_time <= CONFIG["STALE_GAP_SECS"]):
            delta = ask - state.prev_ask
            if (abs(delta) >= CONFIG["MOVE_ALERT_CENTS"]
                    and now - state.last_move_alert >= CONFIG["MOVE_COOLDOWN_SECS"]):
                alert("MOVEMENT", name, ticker, bid, ask, last, delta=delta)
                state.last_move_alert = now

        state.prev_ask = ask
        state.prev_time = now

    log_csv(csv_rows)
    print(f"{ts} | " + "  ".join(status_bits), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Monitor Kalshi YES prices for watched attendance markets.")
    parser.add_argument("--discover", action="store_true",
                        help="print all event markets + watch-list matches, then exit")
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

    matched = match_watchlist(markets)  # exits loudly if any name unmatched
    print_discovery(markets, matched)
    if args.discover:
        return 0

    name_to_ticker = {name: m["ticker"] for name, m in matched.items()}
    states = {ticker: MarketState() for ticker in name_to_ticker.values()}

    channel = ("Discord webhook"
               if (CONFIG["DISCORD_WEBHOOK_URL"]
                   or os.environ.get("DISCORD_WEBHOOK_URL"))
               else "desktop notification + sound")
    print(f"\nAlert channel: {channel}")
    print(f"CSV history:   {CONFIG['CSV_PATH']}")
    print(f"Polling every {CONFIG['POLL_SECS']}s. Ctrl-C to stop.\n")

    backoff_idx = 0
    while True:
        try:
            run_cycle(session, name_to_ticker, states)
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
