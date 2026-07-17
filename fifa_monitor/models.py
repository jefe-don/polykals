"""Product data model and parsers for Shopify JSON + sitemap payloads."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Variant:
    id: int
    title: str
    price: str
    available: bool


@dataclass
class Product:
    id: int
    title: str
    handle: str
    product_type: str = ""
    tags: list[str] = field(default_factory=list)
    variants: list[Variant] = field(default_factory=list)
    image: str = ""

    @property
    def any_available(self) -> bool:
        return any(v.available for v in self.variants)

    @property
    def min_price(self) -> str:
        prices = [v.price for v in self.variants if v.price]
        if not prices:
            return "?"
        try:
            return min(prices, key=lambda p: float(p))
        except ValueError:
            return prices[0]

    def url(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/products/{self.handle}"

    def matches_keywords(self, keywords: list[str]) -> bool:
        haystack = " ".join(
            [self.title, self.handle, self.product_type, " ".join(self.tags)]
        ).lower()
        return any(kw.lower() in haystack for kw in keywords)


def _coerce_tags(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    return []


def parse_products_json(payload: dict[str, Any]) -> list[Product]:
    """Parse a Shopify ``/products.json`` style payload."""
    products: list[Product] = []
    for item in payload.get("products", []):
        try:
            pid = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue

        variants = []
        for v in item.get("variants", []):
            try:
                variants.append(
                    Variant(
                        id=int(v["id"]),
                        title=str(v.get("title", "")),
                        price=str(v.get("price", "")),
                        available=bool(v.get("available", False)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue

        image = ""
        img = item.get("image")
        if isinstance(img, dict):
            image = img.get("src", "") or ""
        if not image:
            images = item.get("images") or []
            if images and isinstance(images[0], dict):
                image = images[0].get("src", "") or ""
            elif images and isinstance(images[0], str):
                image = images[0]

        products.append(
            Product(
                id=pid,
                title=str(item.get("title", "")),
                handle=str(item.get("handle", "")),
                product_type=str(item.get("product_type", "")),
                tags=_coerce_tags(item.get("tags")),
                variants=variants,
                image=image,
            )
        )
    return products


def parse_sitemap_index(xml_text: str) -> list[str]:
    """Return child ``<loc>`` URLs from a ``<sitemapindex>`` document.

    Empty if the document is not an index (e.g. it is a leaf ``<urlset>``).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    if local(root.tag) != "sitemapindex":
        return []

    locs: list[str] = []
    for sm in root:
        if local(sm.tag) != "sitemap":
            continue
        for child in sm:
            if local(child.tag) == "loc" and child.text:
                locs.append(child.text.strip())
    return locs


_HANDLE_RE = re.compile(r"/products/([^/?#]+)")


def parse_sitemap(xml_text: str, base_url: str) -> list[Product]:
    """Parse a Shopify product sitemap into partial ``Product`` records.

    Sitemaps only expose the product handle (and sometimes an image), never
    the numeric id, so these records carry ``id=0`` and are matched on handle.
    """
    products: list[Product] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return products

    # Namespaces vary; strip them by matching on the local tag name.
    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    for url_el in root:
        if local(url_el.tag) != "url":
            continue
        loc = ""
        image = ""
        title = ""
        for child in url_el:
            name = local(child.tag)
            if name == "loc" and child.text:
                loc = child.text.strip()
            else:
                for sub in child:
                    subname = local(sub.tag)
                    if subname == "loc" and sub.text and not image:
                        image = sub.text.strip()
                    elif subname == "title" and sub.text:
                        title = sub.text.strip()

        m = _HANDLE_RE.search(loc)
        if not m:
            continue
        handle = m.group(1)
        products.append(
            Product(
                id=0,
                title=title or handle.replace("-", " ").title(),
                handle=handle,
                image=image,
            )
        )
    return products
