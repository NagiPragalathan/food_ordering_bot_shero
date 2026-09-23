"""Meta Commerce catalogue - the menu source (spec step 7).

Dish names, prices and images all come from here, so the catalogue is the
single source of truth for pricing shown at the menu and on the order summary.

Results are cached in-process for META_CATALOG_CACHE_TTL_SECONDS: the menu
changes rarely but is read on every conversation, and the Graph API rate limit
is per app, not per user.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.core.config import settings
from app.core.logging import get_logger
from app.integrations.base import ApiClient

log = get_logger(__name__)

PRODUCT_FIELDS = (
    "id,retailer_id,name,description,price,currency,image_url,availability,"
    "product_type,custom_label_0,custom_label_1"
)
PAGE_SIZE = 100
# Guards against an unbounded paging loop if the API keeps handing back a cursor.
MAX_PAGES = 20


@dataclass(frozen=True)
class Product:
    retailer_id: str
    name: str
    price: Decimal
    currency: str = "USD"
    description: str = ""
    image_url: str = ""
    availability: str = "in stock"
    cuisine: str = ""
    outlet_code: str = ""

    @property
    def is_available(self) -> bool:
        return self.availability.replace("_", " ").lower() == "in stock"

    @property
    def price_display(self) -> str:
        symbol = {"USD": "$", "INR": "\u20b9", "EUR": "\u20ac", "GBP": "\u00a3"}.get(
            self.currency.upper(), ""
        )
        return f"{symbol}{self.price:.2f}"


class MetaCatalogClient(ApiClient):
    service = "meta_catalog"

    def __init__(self) -> None:
        super().__init__(base_url=settings.meta_graph_url)
        self._cache: list[Product] = []
        self._cached_at: float = 0.0

    async def default_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {settings.meta_system_user_token}"}

    def _cache_is_fresh(self) -> bool:
        return bool(self._cache) and (
            time.time() - self._cached_at < settings.meta_catalog_cache_ttl_seconds
        )

    def invalidate_cache(self) -> None:
        self._cache = []
        self._cached_at = 0.0

    async def list_products(self, *, force_refresh: bool = False) -> list[Product]:
        if not force_refresh and self._cache_is_fresh():
            return self._cache

        products: list[Product] = []
        path = f"/{settings.meta_catalog_id}/products"
        params: dict | None = {"fields": PRODUCT_FIELDS, "limit": PAGE_SIZE}

        for _ in range(MAX_PAGES):
            payload = await self.get(path, params=params)
            for raw in (payload or {}).get("data", []):
                product = _parse_product(raw)
                if product:
                    products.append(product)

            next_url = ((payload or {}).get("paging") or {}).get("next")
            if not next_url:
                break
            # The cursor URL is absolute and already carries its own query.
            path, params = next_url, None

        self._cache = products
        self._cached_at = time.time()
        log.info("meta_catalog_loaded", count=len(products))
        return products

    async def list_by_cuisine(self, cuisine: str) -> list[Product]:
        """Menu for the chosen cuisine (spec step 7).

        An item with no cuisine label is treated as available to every cuisine
        rather than hidden, so a mislabelled row never silently vanishes from
        the menu.
        """
        wanted = (cuisine or "").strip().lower()
        products = await self.list_products()
        if not wanted:
            return [p for p in products if p.is_available]
        return [
            p for p in products
            if p.is_available and (not p.cuisine or p.cuisine.lower() == wanted)
        ]

    async def get_by_retailer_ids(self, retailer_ids: list[str]) -> dict[str, Product]:
        """Look up cart lines by retailer id (spec step 8 availability check)."""
        wanted = set(retailer_ids)
        return {
            p.retailer_id: p
            for p in await self.list_products()
            if p.retailer_id in wanted
        }

    async def available_cuisines(self) -> list[str]:
        """Distinct cuisine labels present in the catalogue.

        The spec leaves the third cuisine "to be confirmed", so the list is
        derived from the data instead of hardcoded.
        """
        seen: dict[str, None] = {}
        for product in await self.list_products():
            if product.is_available and product.cuisine:
                seen.setdefault(product.cuisine.title(), None)
        return list(seen)


_PRICE_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


def parse_price(raw: object) -> Decimal:
    """Parse a Graph API price into a Decimal.

    Graph returns either a formatted string ("$12.00", "1.234,50 EUR") or an
    integer in minor units, depending on how the catalogue was populated.
    A comma decimal separator is normalised; thousands separators are dropped.
    """
    if raw is None:
        return Decimal("0.00")
    if isinstance(raw, (int, float, Decimal)):
        return Decimal(str(raw)).quantize(Decimal("0.01"))

    text = str(raw).strip()
    match = _PRICE_RE.search(text.replace(",", "") if _looks_thousands(text) else text)
    if not match:
        return Decimal("0.00")
    try:
        return Decimal(match.group().replace(",", ".")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


def _looks_thousands(text: str) -> bool:
    """True for '1,234.50' (comma is a group separator) not '1234,50'."""
    return "," in text and "." in text and text.index(",") < text.index(".")


def _parse_product(raw: dict) -> Product | None:
    retailer_id = raw.get("retailer_id") or raw.get("id")
    if not retailer_id:
        return None
    # Cuisine may be tagged as product_type or custom_label_0; outlet on
    # custom_label_1. Both are conventions to confirm with the client.
    return Product(
        retailer_id=str(retailer_id),
        name=raw.get("name") or "Item",
        price=parse_price(raw.get("price")),
        currency=(raw.get("currency") or settings.stripe_currency).upper(),
        description=raw.get("description") or "",
        image_url=raw.get("image_url") or "",
        availability=raw.get("availability") or "in stock",
        cuisine=(raw.get("product_type") or raw.get("custom_label_0") or "").strip(),
        outlet_code=(raw.get("custom_label_1") or "").strip(),
    )


meta_catalog = MetaCatalogClient()
