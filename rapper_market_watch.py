#!/usr/bin/env python3
"""rapper_market_watch.py — watch Kalshi and Polymarket for new rapper markets.

Monitors both platforms' public read-only APIs for prediction markets that
mention a watchlist of artists (Kanye West/Ye, Travis Scott, Young Thug,
Future, Lil Wayne, Lil Baby, Playboi Carti). Seen market/event IDs are
persisted to a local JSON file so only genuinely new markets trigger alerts.

Usage:
    python3 rapper_market_watch.py          # single check-and-exit (cron mode)
    python3 rapper_market_watch.py --loop   # run forever

Environment variables (all optional):
    NTFY_TOPIC           ntfy.sh topic name for push alerts
    DISCORD_WEBHOOK_URL  Discord webhook URL for alerts
    CHECK_INTERVAL_SECS  seconds between checks in --loop mode (default 3600)
    STATE_FILE           path to the seen-IDs JSON (default: seen_markets.json
                         next to this script)

Requires: Python 3.10+, requests.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

REQUEST_TIMEOUT = 15          # seconds, per HTTP request
KALSHI_PAGE_SLEEP = 0.5       # polite pause between paginated Kalshi requests
KALSHI_MAX_PAGES = 50         # safety cap on pagination
POLYMARKET_PAGES = 3          # pages of 100 per Gamma endpoint, newest first

DEFAULT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "seen_markets.json"
)

# Watchlist. Each entry is (display name, regex).
#
# Most names are case-insensitive word-boundary matches. "Future" and "Ye"
# are deliberately CASE-SENSITIVE whole-word matches: a case-insensitive
# match would fire on every market containing the ordinary words "future"
# or "ye". (Title-cased "Future" can still false-positive occasionally;
# that is the accepted trade-off.)
ARTIST_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Kanye West", re.compile(r"\bkanye\b", re.IGNORECASE)),
    ("Kanye West", re.compile(r"\byeezy\b", re.IGNORECASE)),
    ("Kanye West", re.compile(r"\bYe\b")),                     # case-sensitive
    ("Travis Scott", re.compile(r"\btravis\s+scott\b", re.IGNORECASE)),
    ("Young Thug", re.compile(r"\byoung\s+thug\b", re.IGNORECASE)),
    ("Young Thug", re.compile(r"\bthugger\b", re.IGNORECASE)),
    ("Future", re.compile(r"\bFuture\b")),                     # case-sensitive
    ("Future", re.compile(r"\bfuture\s+hendrix\b", re.IGNORECASE)),
    ("Lil Wayne", re.compile(r"\blil'?\s+wayne\b", re.IGNORECASE)),
    ("Lil Wayne", re.compile(r"\bweezy\b", re.IGNORECASE)),
    ("Lil Baby", re.compile(r"\blil'?\s+baby\b", re.IGNORECASE)),
    ("Playboi Carti", re.compile(r"\bplayboi\s+carti\b", re.IGNORECASE)),
    ("Playboi Carti", re.compile(r"\bcarti\b", re.IGNORECASE)),
]

log = logging.getLogger("rapper_market_watch")


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def find_artists(*texts: str | None) -> list[str]:
    """Return the artists mentioned in any of the given text fields."""
    blob = " ".join(t for t in texts if t)
    if not blob:
        return []
    found: list[str] = []
    for name, pattern in ARTIST_PATTERNS:
        if name not in found and pattern.search(blob):
            found.append(name)
    return found


@dataclass
class Hit:
    """A market/event on one platform that mentions a watched artist."""
    source: str            # "Kalshi" or "Polymarket"
    uid: str               # stable, namespaced ID used for seen-state
    title: str
    url: str
    artists: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def _get_json(session: requests.Session, url: str, params: dict) -> dict | list:
    resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_polymarket_hits(session: requests.Session) -> list[Hit]:
    """Scan Polymarket Gamma /events and /markets, newest first."""
    hits: list[Hit] = []

    # Events (with nested markets)
    for page in range(POLYMARKET_PAGES):
        params = {
            "order": "createdAt",
            "ascending": "false",
            "closed": "false",
            "limit": 100,
            "offset": page * 100,
        }
        events = _get_json(session, f"{POLYMARKET_GAMMA}/events", params)
        if not isinstance(events, list) or not events:
            break
        for ev in events:
            if not isinstance(ev, dict):
                continue
            slug = ev.get("slug") or ""
            url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"
            title = ev.get("title") or "(untitled event)"

            artists = find_artists(ev.get("title"), ev.get("description"))
            if artists and ev.get("id"):
                hits.append(Hit("Polymarket", f"poly:event:{ev['id']}", title, url, artists))

            for mkt in ev.get("markets") or []:
                if not isinstance(mkt, dict) or not mkt.get("id"):
                    continue
                m_artists = find_artists(mkt.get("question"), mkt.get("description"))
                if m_artists:
                    q = mkt.get("question") or "(untitled market)"
                    hits.append(Hit(
                        "Polymarket", f"poly:market:{mkt['id']}",
                        f"{title} — {q}" if q != title else title,
                        url, m_artists,
                    ))

    # Standalone /markets sweep (catches new markets added to older events)
    for page in range(POLYMARKET_PAGES):
        params = {
            "order": "createdAt",
            "ascending": "false",
            "closed": "false",
            "limit": 100,
            "offset": page * 100,
        }
        markets = _get_json(session, f"{POLYMARKET_GAMMA}/markets", params)
        if not isinstance(markets, list) or not markets:
            break
        for mkt in markets:
            if not isinstance(mkt, dict) or not mkt.get("id"):
                continue
            artists = find_artists(mkt.get("question"), mkt.get("description"))
            if not artists:
                continue
            # Prefer the parent event's slug for the URL; fall back to the
            # market's own slug (event pages also resolve market slugs).
            events = mkt.get("events") or []
            ev0 = events[0] if events and isinstance(events[0], dict) else {}
            slug = ev0.get("slug") or mkt.get("slug") or ""
            url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"
            hits.append(Hit(
                "Polymarket", f"poly:market:{mkt['id']}",
                mkt.get("question") or "(untitled market)", url, artists,
            ))

    return hits


def fetch_kalshi_hits(session: requests.Session) -> list[Hit]:
    """Scan all open Kalshi events (with nested markets), paginating by cursor."""
    hits: list[Hit] = []
    cursor: str | None = None

    for page in range(KALSHI_MAX_PAGES):
        params: dict = {
            "status": "open",
            "with_nested_markets": "true",
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor
        data = _get_json(session, f"{KALSHI_API}/events", params)
        if not isinstance(data, dict):
            break

        for ev in data.get("events") or []:
            if not isinstance(ev, dict):
                continue
            ticker = ev.get("event_ticker") or ""
            if not ticker:
                continue
            url = f"https://kalshi.com/events/{ticker}"
            title = ev.get("title") or ticker

            artists = find_artists(ev.get("title"), ev.get("sub_title"))
            if artists:
                hits.append(Hit("Kalshi", f"kalshi:event:{ticker}", title, url, artists))

            for mkt in ev.get("markets") or []:
                if not isinstance(mkt, dict) or not mkt.get("ticker"):
                    continue
                m_artists = find_artists(
                    mkt.get("title"), mkt.get("subtitle"), mkt.get("yes_sub_title")
                )
                if m_artists:
                    m_title = mkt.get("title") or mkt.get("ticker")
                    hits.append(Hit(
                        "Kalshi", f"kalshi:market:{mkt['ticker']}",
                        f"{title} — {m_title}" if m_title != title else title,
                        url, m_artists,
                    ))

        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(KALSHI_PAGE_SLEEP)
    else:
        log.warning("Kalshi pagination hit the %d-page safety cap", KALSHI_MAX_PAGES)

    return hits


# ---------------------------------------------------------------------------
# Seen-state persistence
# ---------------------------------------------------------------------------

def load_state(path: str) -> dict | None:
    """Return the state dict, or None if this is the first run."""
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        if isinstance(state, dict) and isinstance(state.get("seen_ids"), list):
            return state
        log.warning("State file %s is malformed; treating as first run", path)
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read state file %s (%s); treating as first run", path, exc)
    return None


def save_state(path: str, seen_ids: set[str], baseline_at: str) -> None:
    state = {"baseline_at": baseline_at, "seen_ids": sorted(seen_ids)}
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

def send_ntfy(hit: Hit) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    try:
        resp = requests.post(
            f"https://ntfy.sh/{topic}",
            data=f"{hit.title}\n{hit.url}".encode(),
            headers={
                "Title": f"New {hit.source} market: {', '.join(hit.artists)}",
                "Click": hit.url,
                "Tags": "rotating_light,microphone",
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("ntfy alert failed: %s", exc)


def send_discord(hit: Hit) -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        return
    try:
        resp = requests.post(
            webhook,
            json={
                "content": (
                    f"🚨 **New {hit.source} market** ({', '.join(hit.artists)})\n"
                    f"{hit.title}\n{hit.url}"
                )
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Discord alert failed: %s", exc)


def alert(hit: Hit) -> None:
    print(f"[NEW] {hit.source} | {hit.title} (matched: {', '.join(hit.artists)})")
    print(f"      {hit.url}")
    send_ntfy(hit)
    send_discord(hit)


# ---------------------------------------------------------------------------
# Main check cycle
# ---------------------------------------------------------------------------

def run_check(state_path: str) -> None:
    session = requests.Session()
    session.headers["User-Agent"] = "rapper-market-watch/1.0"

    hits: list[Hit] = []
    platforms_ok = 0
    for name, fetcher in (("Polymarket", fetch_polymarket_hits),
                          ("Kalshi", fetch_kalshi_hits)):
        try:
            hits.extend(fetcher(session))
            platforms_ok += 1
        except requests.RequestException as exc:
            log.warning("%s fetch failed, skipping this platform: %s", name, exc)
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("%s returned unexpected data, skipping: %s", name, exc)

    if platforms_ok == 0:
        log.warning("Both platforms failed; nothing to do this cycle")
        return

    # De-duplicate hits within this run (same market can surface via both
    # the /events and /markets sweeps on Polymarket).
    unique: dict[str, Hit] = {}
    for hit in hits:
        unique.setdefault(hit.uid, hit)
    hits = list(unique.values())

    state = load_state(state_path)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # An event and its lone market often share the same title/URL; show each
    # distinct (source, title, url) once, while every uid still enters state.
    def display_hits(candidates: list[Hit]) -> list[Hit]:
        shown: set[tuple[str, str, str]] = set()
        out = []
        for h in sorted(candidates, key=lambda h: (h.source, h.title)):
            key = (h.source, h.title, h.url)
            if key not in shown:
                shown.add(key)
                out.append(h)
        return out

    if state is None:
        # First run: record everything currently live as the baseline.
        print(f"First run — recording baseline of {len(hits)} matching "
              f"market(s)/event(s). No alerts sent.")
        for hit in display_hits(hits):
            print(f"[BASELINE] {hit.source} | {hit.title} "
                  f"(matched: {', '.join(hit.artists)})")
            print(f"           {hit.url}")
        save_state(state_path, {h.uid for h in hits}, baseline_at=now)
        print(f"Baseline written to {state_path}")
        return

    seen: set[str] = set(state["seen_ids"])
    new_hits = [h for h in hits if h.uid not in seen]

    if new_hits:
        for hit in display_hits(new_hits):
            alert(hit)
        seen.update(h.uid for h in new_hits)
        save_state(state_path, seen, baseline_at=state.get("baseline_at") or now)
        print(f"{len(new_hits)} new market(s); state updated ({state_path})")
    else:
        print(f"No new markets ({len(hits)} known matching market(s) still live)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Watch Kalshi and Polymarket for new rapper prediction markets."
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="run forever, checking every CHECK_INTERVAL_SECS (default 3600)",
    )
    parser.add_argument(
        "--test-alert", action="store_true",
        help="send a test alert through stdout/ntfy/Discord and exit",
    )
    args = parser.parse_args()

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    state_path = os.environ.get("STATE_FILE", DEFAULT_STATE_FILE)

    if args.test_alert:
        channels = ["stdout"]
        if os.environ.get("NTFY_TOPIC"):
            channels.append("ntfy")
        if os.environ.get("DISCORD_WEBHOOK_URL"):
            channels.append("Discord")
        print(f"Sending test alert via: {', '.join(channels)}")
        if len(channels) == 1:
            print("(set NTFY_TOPIC and/or DISCORD_WEBHOOK_URL to test push alerts)")
        alert(Hit(
            "Test", "test:alert",
            "Test alert — rapper_market_watch is configured correctly",
            "https://github.com/jefe-don/polykals", ["Kanye West"],
        ))
        return 0

    if not args.loop:
        run_check(state_path)
        return 0

    try:
        interval = int(os.environ.get("CHECK_INTERVAL_SECS", "3600"))
    except ValueError:
        log.warning("Invalid CHECK_INTERVAL_SECS; falling back to 3600")
        interval = 3600

    print(f"Loop mode: checking every {interval}s. Ctrl-C to stop.")
    while True:
        try:
            run_check(state_path)
        except Exception:  # noqa: BLE001 — one bad cycle must not kill the loop
            log.exception("Check cycle failed; will retry next interval")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopping.")
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopping.")
        sys.exit(0)
