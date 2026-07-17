"""Core monitor: concurrent endpoint loops, detection, and dispatch."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import timedelta

from .config import Config
from .discord import DiscordNotifier
from .http_client import ClientPool
from urllib.parse import urlparse

from .models import (
    Product,
    parse_products_json,
    parse_sitemap,
    parse_sitemap_index,
)
from .proxies import ProxyPool, load_proxies
from .state import State

log = logging.getLogger("fifa_monitor.monitor")

# Status codes that indicate we should back off and blame the proxy.
BLOCK_CODES = {403, 429, 430}


class Monitor:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.state = State(config.state_file)
        self.clients = ClientPool(config.request_timeout_s)
        self.proxies = ProxyPool(
            load_proxies(config.proxies_file),
            fail_threshold=config.proxy_fail_threshold,
            bench_seconds=config.proxy_bench_seconds,
        )
        self.discord = DiscordNotifier(
            webhook_url=config.webhook_url,
            base_url=config.base_url,
            mention=config.mention,
            heartbeat_webhook_url=config.heartbeat_webhook_url,
        )
        self._stop = asyncio.Event()
        self._backoff: dict[str, float] = {}

        # Stats
        self._start = time.time()
        self.requests = 0
        self.errors = 0
        self.alerts = 0

    # -------------------------------------------------------------- baseline
    async def baseline(self) -> None:
        """Fetch every endpoint once and record known products without alerting."""
        if self.state.initialized:
            log.info("State already initialised; skipping baseline (no re-alerts).")
            return

        log.info("Building baseline (no alerts will be sent)…")
        for ep in self.cfg.endpoints:
            proxy = self.proxies.next()
            result = await self.clients.fetch(ep["url"], proxy)
            if not result.ok or not result.text:
                log.warning(
                    "Baseline fetch of %s failed (status=%s err=%s).",
                    ep["name"], result.status, result.error,
                )
                continue
            self.state.set_cache(ep["name"], result.etag, result.last_modified)
            if ep.get("type") == "sitemap":
                products = await self._sitemap_products(result.text, proxy)
            else:
                products = self._parse(ep, result.text)
            self._seed(products)
            self.state.baselined_endpoints.add(ep["name"])
            log.info("Baseline %s: %d products.", ep["name"], len(products))

        self.state.initialized = True
        self.state.save()
        log.info(
            "Baseline complete: %d known ids, %d handles.",
            len(self.state.known_ids), len(self.state.known_handles),
        )

    # ------------------------------------------------------------------ parse
    def _parse(self, ep: dict, text: str) -> list[Product]:
        if ep.get("type") == "sitemap":
            return parse_sitemap(text, self.cfg.base_url)
        import json

        try:
            return parse_products_json(json.loads(text))
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("Failed to parse JSON from %s: %s", ep["name"], exc)
            return []

    async def _sitemap_products(self, text: str, proxy) -> list[Product]:
        """Parse a sitemap; if it is an index, follow product child sitemaps."""
        products = parse_sitemap(text, self.cfg.base_url)
        if products:
            return products

        # Canonical (non-locale-prefixed) product child sitemaps only.
        children = [
            loc
            for loc in parse_sitemap_index(text)
            if urlparse(loc).path.startswith("/sitemap_products")
        ]
        collected: list[Product] = []
        for loc in children[: self.cfg.sitemap_max_children]:
            child = await self.clients.fetch(loc, proxy)
            self.requests += 1
            if child.ok and child.text:
                collected.extend(parse_sitemap(child.text, self.cfg.base_url))
            else:
                log.warning(
                    "Child sitemap fetch failed (status=%s err=%s): %s",
                    child.status, child.error, loc,
                )
        return collected

    # -------------------------------------------------------------- detection
    def _seed(self, products: list[Product]) -> None:
        """Record products as known WITHOUT alerting (baseline seeding)."""
        for p in products:
            if p.id:
                self.state.known_ids.add(p.id)
                self.state.availability[p.id] = p.any_available
            if p.handle:
                self.state.known_handles.add(p.handle)

    def _dispatch(self, product: Product, alert_type: str, key: str) -> None:
        if self.state.already_sent(alert_type, key):
            return
        self.state.mark_sent(alert_type, key)
        self.discord.send_alert(product, alert_type)
        self.alerts += 1

    def process_products_json(self, products: list[Product]) -> bool:
        changed = False
        keywords = self.cfg.keywords
        for p in products:
            is_new = p.id not in self.state.known_ids
            matches = p.matches_keywords(keywords)
            prev_avail = self.state.availability.get(p.id)

            if is_new and matches:
                self._dispatch(p, "keyword", str(p.id))
                changed = True
            elif is_new:
                self._dispatch(p, "new_product", str(p.id))
                changed = True
            elif matches and prev_avail is False and p.any_available:
                # Known keyword product flipped unavailable -> available.
                self._dispatch(p, "restock", str(p.id))
                changed = True

            if p.id and (is_new or self.state.availability.get(p.id) != p.any_available):
                changed = True
            if p.id:
                self.state.known_ids.add(p.id)
                self.state.availability[p.id] = p.any_available
            if p.handle:
                self.state.known_handles.add(p.handle)
        return changed

    def process_sitemap(self, products: list[Product]) -> bool:
        changed = False
        keywords = self.cfg.keywords
        for p in products:
            if not p.handle or p.handle in self.state.known_handles:
                continue
            # New handle seen in the sitemap before the JSON endpoints caught it.
            alert_type = "keyword" if p.matches_keywords(keywords) else "new_product"
            self._dispatch(p, alert_type, f"h:{p.handle}")
            self.state.known_handles.add(p.handle)
            changed = True
        return changed

    # ------------------------------------------------------------ poll loops
    def _interval_s(self, ep: dict) -> float:
        base = ep.get("interval_ms", self.cfg.default_interval_ms) / 1000.0
        jitter = base * self.cfg.jitter_pct
        return max(0.1, base + random.uniform(-jitter, jitter))

    async def _endpoint_loop(self, ep: dict) -> None:
        name = ep["name"]
        while not self._stop.is_set():
            proxy = self.proxies.next()
            cache = self.state.get_cache(name)
            result = await self.clients.fetch(
                ep["url"], proxy, cache.get("etag", ""), cache.get("last_modified", "")
            )
            self.requests += 1
            proxy_key = proxy.key if proxy else "direct"

            if result.error:
                self.errors += 1
                self.proxies.record_failure(proxy, result.error)
                log.warning(
                    "[%s] %s via %s (%.0fms)",
                    name, result.error, proxy_key, result.latency_ms,
                )
                await self._sleep_with_backoff(name, ep)
                continue

            log.info(
                "[%s] HTTP %s via %s (%.0fms)%s",
                name, result.status, proxy_key, result.latency_ms,
                " not-modified" if result.not_modified else "",
            )

            if result.status in BLOCK_CODES or result.status >= 500:
                self.errors += 1
                self.proxies.record_failure(proxy, f"status={result.status}")
                await self._sleep_with_backoff(name, ep)
                continue

            self.proxies.record_success(proxy)
            self._backoff.pop(name, None)  # reset backoff on success

            if result.status == 200 and result.text:
                self.state.set_cache(name, result.etag, result.last_modified)
                if ep.get("type") == "sitemap":
                    products = await self._sitemap_products(result.text, proxy)
                else:
                    products = self._parse(ep, result.text)

                if name not in self.state.baselined_endpoints:
                    # First successful response for an endpoint that could not
                    # be baselined at startup (e.g. it was blocked by 429/503).
                    # Seed it silently so recovery never floods with "new"
                    # alerts for the whole catalogue.
                    self._seed(products)
                    self.state.baselined_endpoints.add(name)
                    self.state.save()
                    log.info(
                        "[%s] late baseline seeded %d products (no alerts).",
                        name, len(products),
                    )
                else:
                    if ep.get("type") == "sitemap":
                        changed = self.process_sitemap(products)
                    else:
                        changed = self.process_products_json(products)
                    if changed:
                        self.state.save()

            await self._sleep(self._interval_s(ep))

    async def _sleep_with_backoff(self, name: str, ep: dict) -> None:
        current = self._backoff.get(name, 0.0)
        if current <= 0:
            nxt = self.cfg.backoff_base_s
        else:
            nxt = min(current * 2, self.cfg.backoff_cap_s)
        self._backoff[name] = nxt
        log.info("[%s] backing off %.1fs.", name, nxt)
        await self._sleep(nxt)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------- heartbeat
    async def _heartbeat_loop(self) -> None:
        if not self.cfg.heartbeat_enabled:
            return
        while not self._stop.is_set():
            await self._sleep(self.cfg.heartbeat_interval_s)
            if self._stop.is_set():
                break
            self.discord.send_heartbeat(self._stats())

    def _stats(self) -> dict:
        uptime = timedelta(seconds=int(time.time() - self._start))
        rate = (self.errors / self.requests * 100) if self.requests else 0.0
        return {
            "uptime": str(uptime),
            "requests": self.requests,
            "errors": self.errors,
            "error_rate": f"{rate:.1f}%",
            "alerts": self.alerts,
            "known": len(self.state.known_ids),
        }

    # ------------------------------------------------------------------- run
    async def run(self) -> None:
        self.state.load()
        await self.baseline()
        log.info("Starting monitor loops for %d endpoints.", len(self.cfg.endpoints))
        tasks = [
            asyncio.ensure_future(self._endpoint_loop(ep)) for ep in self.cfg.endpoints
        ]
        tasks.append(asyncio.ensure_future(self._heartbeat_loop()))
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.shutdown()

    def stop(self) -> None:
        log.info("Shutdown requested; stopping loops…")
        self._stop.set()

    async def shutdown(self) -> None:
        self.state.save()
        await self.discord.aclose()
        await self.clients.aclose()
        log.info("State saved. Goodbye. Final stats: %s", self._stats())
