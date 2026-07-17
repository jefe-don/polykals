"""HTTP client pool with per-proxy reuse, UA rotation, conditional requests."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

import httpx

from .proxies import Proxy

log = logging.getLogger("fifa_monitor.http")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

DIRECT_KEY = "__direct__"


def base_headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
    }


@dataclass
class FetchResult:
    status: int
    text: str
    etag: str
    last_modified: str
    latency_ms: float
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.error == "" and self.status in (200, 304)

    @property
    def not_modified(self) -> bool:
        return self.status == 304


class ClientPool:
    """Lazily builds and reuses one ``httpx.AsyncClient`` per proxy."""

    def __init__(self, timeout_s: float = 4.0) -> None:
        self._timeout = httpx.Timeout(timeout_s)
        self._clients: dict[str, httpx.AsyncClient] = {}

    def _client_for(self, proxy: Proxy | None) -> httpx.AsyncClient:
        key = proxy.key if proxy is not None else DIRECT_KEY
        client = self._clients.get(key)
        if client is None:
            kwargs = {
                "timeout": self._timeout,
                "http2": True,
                "follow_redirects": True,
                "limits": httpx.Limits(max_keepalive_connections=4, max_connections=8),
            }
            if proxy is not None:
                kwargs["proxy"] = proxy.url()
            client = httpx.AsyncClient(**kwargs)
            self._clients[key] = client
        return client

    async def fetch(
        self,
        url: str,
        proxy: Proxy | None,
        etag: str = "",
        last_modified: str = "",
    ) -> FetchResult:
        headers = base_headers()
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        client = self._client_for(proxy)
        start = time.perf_counter()
        try:
            resp = await client.get(url, headers=headers)
        except httpx.TimeoutException:
            latency = (time.perf_counter() - start) * 1000
            return FetchResult(0, "", "", "", latency, error="timeout")
        except httpx.HTTPError as exc:
            latency = (time.perf_counter() - start) * 1000
            return FetchResult(0, "", "", "", latency, error=f"http_error:{exc}")

        latency = (time.perf_counter() - start) * 1000
        text = "" if resp.status_code == 304 else resp.text
        return FetchResult(
            status=resp.status_code,
            text=text,
            etag=resp.headers.get("ETag", ""),
            last_modified=resp.headers.get("Last-Modified", ""),
            latency_ms=latency,
        )

    async def aclose(self) -> None:
        for client in self._clients.values():
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - best effort on shutdown
                pass
