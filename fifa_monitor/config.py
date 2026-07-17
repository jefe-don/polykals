"""Configuration loading and defaults."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_ENDPOINTS = [
    {
        "name": "collectibles",
        "url": "https://store.fifa.com/collections/collectibles/products.json?limit=250",
        "type": "products_json",
        "interval_ms": 1500,
    },
    {
        "name": "all_products",
        "url": "https://store.fifa.com/products.json?limit=250&order=created_at:desc",
        "type": "products_json",
        "interval_ms": 1500,
    },
    {
        # The sitemap index; the monitor follows its product child sitemaps.
        # (A fixed sitemap_products_1.xml?from=1&to=... URL returns only the
        # homepage because Shopify shifts the from/to bounds as products change.)
        "name": "sitemap",
        "url": "https://store.fifa.com/sitemap.xml",
        "type": "sitemap",
        "interval_ms": 1500,
    },
]


@dataclass
class Config:
    keywords: list[str] = field(default_factory=lambda: ["ring"])
    endpoints: list[dict[str, Any]] = field(
        default_factory=lambda: [dict(e) for e in DEFAULT_ENDPOINTS]
    )
    default_interval_ms: int = 1500
    jitter_pct: float = 0.30
    request_timeout_s: float = 4.0
    base_url: str = "https://store.fifa.com"
    # When a sitemap URL points at a <sitemapindex>, follow up to this many
    # canonical product child sitemaps per poll.
    sitemap_max_children: int = 3

    # Discord
    mention: str = "@everyone"  # used for keyword hits + restocks
    heartbeat_enabled: bool = True
    heartbeat_interval_s: int = 1800

    # Proxy health
    proxy_bench_seconds: int = 300
    proxy_fail_threshold: int = 3

    # Backoff
    backoff_base_s: float = 2.0
    backoff_cap_s: float = 60.0

    # Files
    state_file: str = "state.json"
    proxies_file: str = "proxies.txt"
    log_file: str = "monitor.log"
    log_level: str = "INFO"

    # Secrets (from environment / .env)
    webhook_url: str = ""
    heartbeat_webhook_url: str = ""

    @classmethod
    def load(cls, path: str | os.PathLike[str] = "config.json") -> "Config":
        cfg = cls()
        p = Path(path)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            for key, value in data.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)

        # Normalise per-endpoint interval defaults.
        for ep in cfg.endpoints:
            ep.setdefault("interval_ms", cfg.default_interval_ms)
            ep.setdefault("type", "products_json")

        # Secrets always come from the environment (never config.json).
        cfg.webhook_url = os.getenv("DISCORD_WEBHOOK_URL", cfg.webhook_url)
        cfg.heartbeat_webhook_url = os.getenv(
            "DISCORD_HEARTBEAT_WEBHOOK_URL", cfg.heartbeat_webhook_url
        )
        return cfg
