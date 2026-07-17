"""Proxy pool: parsing, round-robin rotation, and health tracking."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("fifa_monitor.proxies")


@dataclass
class Proxy:
    host: str
    port: str
    user: str = ""
    password: str = ""

    consecutive_failures: int = 0
    benched_until: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"

    def url(self, scheme: str = "http") -> str:
        auth = f"{self.user}:{self.password}@" if self.user else ""
        return f"{scheme}://{auth}{self.host}:{self.port}"

    def is_benched(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return now < self.benched_until


_IPPORT_UP = re.compile(r"^(?P<host>[^:@\s]+):(?P<port>\d+):(?P<user>[^:@\s]+):(?P<pw>.+)$")
_UP_IPPORT = re.compile(
    r"^(?P<user>[^:@\s]+):(?P<pw>[^@\s]+)@(?P<host>[^:@\s]+):(?P<port>\d+)$"
)
_IPPORT_ONLY = re.compile(r"^(?P<host>[^:@\s]+):(?P<port>\d+)$")


def parse_proxy_line(line: str) -> Proxy | None:
    """Parse a single proxy line, accepting several common formats."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    m = _UP_IPPORT.match(line)
    if m:
        return Proxy(m["host"], m["port"], m["user"], m["pw"])

    m = _IPPORT_UP.match(line)
    if m:
        return Proxy(m["host"], m["port"], m["user"], m["pw"])

    m = _IPPORT_ONLY.match(line)
    if m:
        return Proxy(m["host"], m["port"])

    log.warning("Could not parse proxy line: %r", line)
    return None


def load_proxies(path: str) -> list[Proxy]:
    p = Path(path)
    if not p.exists():
        log.warning("Proxy file %s not found; using direct connection only.", path)
        return []
    proxies: list[Proxy] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        proxy = parse_proxy_line(line)
        if proxy:
            proxies.append(proxy)
    log.info("Loaded %d proxies from %s.", len(proxies), path)
    return proxies


class ProxyPool:
    """Round-robin pool with per-proxy health benching."""

    def __init__(
        self,
        proxies: list[Proxy],
        fail_threshold: int = 3,
        bench_seconds: int = 300,
    ) -> None:
        self.proxies = proxies
        self.fail_threshold = fail_threshold
        self.bench_seconds = bench_seconds
        self._idx = 0
        self._warned_all_benched = False

    def _healthy(self) -> list[Proxy]:
        now = time.time()
        return [p for p in self.proxies if not p.is_benched(now)]

    def next(self) -> Proxy | None:
        """Return the next healthy proxy, or ``None`` to use a direct connection."""
        if not self.proxies:
            return None

        healthy = self._healthy()
        if not healthy:
            if not self._warned_all_benched:
                log.warning(
                    "ALL %d proxies are benched — falling back to DIRECT connection!",
                    len(self.proxies),
                )
                self._warned_all_benched = True
            return None

        self._warned_all_benched = False
        proxy = healthy[self._idx % len(healthy)]
        self._idx += 1
        return proxy

    def record_success(self, proxy: Proxy | None) -> None:
        if proxy is not None:
            proxy.consecutive_failures = 0

    def record_failure(self, proxy: Proxy | None, reason: str = "") -> None:
        if proxy is None:
            return
        proxy.consecutive_failures += 1
        if proxy.consecutive_failures >= self.fail_threshold:
            proxy.benched_until = time.time() + self.bench_seconds
            proxy.consecutive_failures = 0
            log.warning(
                "Benching proxy %s for %ds (%s).",
                proxy.key,
                self.bench_seconds,
                reason or "repeated failures",
            )
