"""Discord webhook alerting: rich embeds, fire-and-forget, heartbeat."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from .models import Product, Variant

log = logging.getLogger("fifa_monitor.discord")

# Alert type -> (embed colour, label, does it @mention?)
ALERT_STYLES = {
    "keyword": (0x2ECC71, "🔔 KEYWORD HIT", True),
    "new_product": (0x3498DB, "🆕 NEW PRODUCT", False),
    "restock": (0xE67E22, "♻️ RESTOCK", True),
    "test": (0x9B59B6, "🧪 TEST ALERT", False),
}


class DiscordNotifier:
    def __init__(
        self,
        webhook_url: str,
        base_url: str,
        mention: str = "@everyone",
        heartbeat_webhook_url: str = "",
    ) -> None:
        self.webhook_url = webhook_url
        self.heartbeat_webhook_url = heartbeat_webhook_url
        self.base_url = base_url
        self.mention = mention
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self._pending: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------- embeds
    def build_embed(self, product: Product, alert_type: str) -> dict[str, Any]:
        color, label, _ = ALERT_STYLES.get(alert_type, ALERT_STYLES["new_product"])
        product_url = product.url(self.base_url)

        variant_lines = []
        cart_lines = []
        for v in product.variants[:20]:
            mark = "✅" if v.available else "❌"
            variant_lines.append(f"{mark} `{v.id}` {v.title} — {v.price}")
            cart_lines.append(
                f"[{v.title or v.id}]({self.base_url.rstrip('/')}/cart/{v.id}:1)"
            )
        if not variant_lines:
            variant_lines = ["_(no variant data — check product page)_"]

        fields = [
            {"name": "Price", "value": f"${product.min_price}", "inline": True},
            {
                "name": "Available",
                "value": "Yes" if product.any_available else "No",
                "inline": True,
            },
            {
                "name": "Variants",
                "value": "\n".join(variant_lines)[:1024],
                "inline": False,
            },
        ]
        if cart_lines:
            fields.append(
                {
                    "name": "Quick add-to-cart",
                    "value": " • ".join(cart_lines)[:1024],
                    "inline": False,
                }
            )

        embed: dict[str, Any] = {
            "title": f"{label}: {product.title}",
            "url": product_url,
            "color": color,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "FIFA Store Monitor"},
        }
        if product.image:
            embed["thumbnail"] = {"url": product.image}
        return embed

    def _payload(self, product: Product, alert_type: str) -> dict[str, Any]:
        _, _, mentions = ALERT_STYLES.get(alert_type, ALERT_STYLES["new_product"])
        payload: dict[str, Any] = {"embeds": [self.build_embed(product, alert_type)]}
        if mentions and self.mention:
            payload["content"] = self.mention
            payload["allowed_mentions"] = {"parse": ["everyone", "roles", "users"]}
        return payload

    # ---------------------------------------------------------- dispatch
    async def _post(
        self, webhook_url: str, payload: dict[str, Any], label: str = "message"
    ) -> None:
        try:
            resp = await self._client.post(webhook_url, json=payload)
            if resp.status_code == 429:
                retry = 1.0
                try:
                    retry = float(resp.json().get("retry_after", 1.0))
                except Exception:  # noqa: BLE001
                    pass
                log.warning("Discord rate limited (%s); retrying in %.1fs.", label, retry)
                await asyncio.sleep(retry)
                resp = await self._client.post(webhook_url, json=payload)
            if resp.status_code >= 300:
                log.error(
                    "Discord %s NOT delivered — HTTP %s: %s "
                    "(is the webhook URL still valid? rotating it invalidates a "
                    "running monitor until you restart it)",
                    label, resp.status_code, resp.text[:200],
                )
            else:
                log.info("Discord %s delivered (HTTP %s).", label, resp.status_code)
        except httpx.HTTPError as exc:
            log.error("Discord %s failed to post: %s", label, exc)

    def send_alert(self, product: Product, alert_type: str) -> None:
        """Fire-and-forget: schedule the webhook post without blocking polling."""
        if not self.webhook_url:
            log.error("No DISCORD_WEBHOOK_URL configured; cannot send alert.")
            return
        payload = self._payload(product, alert_type)
        self._spawn(
            self._post(self.webhook_url, payload, f"{alert_type} alert '{product.title}'")
        )
        log.info("Queued %s alert for %r (id=%s).", alert_type, product.title, product.id)

    def send_startup(self, known: int, proxies: int) -> None:
        """Post a launch confirmation so a broken webhook is obvious immediately."""
        if not self.webhook_url:
            log.error("No DISCORD_WEBHOOK_URL configured; cannot send startup ping.")
            return
        embed = {
            "title": "✅ FIFA Monitor started",
            "color": 0x2ECC71,
            "description": "Now watching for new/restocked products.",
            "fields": [
                {"name": "Known products", "value": str(known), "inline": True},
                {
                    "name": "Proxies",
                    "value": str(proxies) if proxies else "direct",
                    "inline": True,
                },
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._spawn(self._post(self.webhook_url, {"embeds": [embed]}, "startup ping"))

    def send_heartbeat(self, stats: dict[str, Any]) -> None:
        webhook = self.heartbeat_webhook_url or self.webhook_url
        if not webhook:
            return
        embed = {
            "title": "💓 Monitor Heartbeat",
            "color": 0x95A5A6,
            "fields": [
                {"name": "Uptime", "value": stats.get("uptime", "?"), "inline": True},
                {"name": "Requests", "value": str(stats.get("requests", 0)), "inline": True},
                {"name": "Errors", "value": str(stats.get("errors", 0)), "inline": True},
                {"name": "Error rate", "value": stats.get("error_rate", "0%"), "inline": True},
                {"name": "Alerts sent", "value": str(stats.get("alerts", 0)), "inline": True},
                {"name": "Known products", "value": str(stats.get("known", 0)), "inline": True},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._spawn(self._post(webhook, {"embeds": [embed]}, "heartbeat"))

    async def send_test(self) -> None:
        """Synchronously send a fake alert so formatting can be verified."""
        fake = Product(
            id=9999999999,
            title="FIFA World Cup Championship Ring (TEST)",
            handle="fifa-world-cup-championship-ring",
            product_type="Collectibles",
            tags=["ring", "limited", "collectible"],
            image="https://store.fifa.com/cdn/shop/files/placeholder.png",
            variants=[
                Variant(id=1111, title="Size 9", price="499.00", available=True),
                Variant(id=2222, title="Size 10", price="499.00", available=False),
            ],
        )
        await self._post(self.webhook_url, self._payload(fake, "test"))
        log.info("Test alert sent to webhook.")

    # ----------------------------------------------------------- lifecycle
    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self) -> None:
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def aclose(self) -> None:
        await self.drain()
        await self._client.aclose()
