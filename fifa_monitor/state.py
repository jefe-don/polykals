"""Persistent state: baseline ids/handles, sent alerts, availability, caching."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any

log = logging.getLogger("fifa_monitor.state")


class State:
    """In-memory state with atomic persistence to ``state.json``."""

    def __init__(self, path: str = "state.json") -> None:
        self.path = Path(path)
        self._lock = Lock()
        self.known_ids: set[int] = set()
        self.known_handles: set[str] = set()
        # alert_type -> set of "product identity" keys already alerted
        self.sent_alerts: dict[str, set[str]] = {}
        # product_id -> was any variant available at last observation
        self.availability: dict[int, bool] = {}
        # endpoint name -> {"etag": str, "last_modified": str}
        self.http_cache: dict[str, dict[str, str]] = {}
        # endpoints whose baseline has been seeded (no alerts fired for them yet)
        self.baselined_endpoints: set[str] = set()
        self.initialized: bool = False

    # ------------------------------------------------------------------ load
    def load(self) -> None:
        if not self.path.exists():
            log.info("No state file at %s; starting fresh.", self.path)
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read state file (%s); starting fresh.", exc)
            return

        self.known_ids = {int(x) for x in data.get("known_ids", [])}
        self.known_handles = set(data.get("known_handles", []))
        self.sent_alerts = {
            k: set(v) for k, v in data.get("sent_alerts", {}).items()
        }
        self.availability = {
            int(k): bool(v) for k, v in data.get("availability", {}).items()
        }
        self.http_cache = data.get("http_cache", {})
        self.baselined_endpoints = set(data.get("baselined_endpoints", []))
        self.initialized = bool(data.get("initialized", False))
        log.info(
            "Loaded state: %d known ids, %d handles, %d alert types.",
            len(self.known_ids),
            len(self.known_handles),
            len(self.sent_alerts),
        )

    # ------------------------------------------------------------------ save
    def save(self) -> None:
        with self._lock:
            data: dict[str, Any] = {
                "known_ids": sorted(self.known_ids),
                "known_handles": sorted(self.known_handles),
                "sent_alerts": {k: sorted(v) for k, v in self.sent_alerts.items()},
                "availability": {str(k): v for k, v in self.availability.items()},
                "http_cache": self.http_cache,
                "baselined_endpoints": sorted(self.baselined_endpoints),
                "initialized": self.initialized,
            }
            self._atomic_write(data)

    def _atomic_write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent or "."), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    # --------------------------------------------------------------- helpers
    def already_sent(self, alert_type: str, key: str) -> bool:
        return key in self.sent_alerts.get(alert_type, set())

    def mark_sent(self, alert_type: str, key: str) -> None:
        self.sent_alerts.setdefault(alert_type, set()).add(key)

    def get_cache(self, endpoint: str) -> dict[str, str]:
        return self.http_cache.get(endpoint, {})

    def set_cache(self, endpoint: str, etag: str = "", last_modified: str = "") -> None:
        entry = self.http_cache.setdefault(endpoint, {})
        if etag:
            entry["etag"] = etag
        if last_modified:
            entry["last_modified"] = last_modified
