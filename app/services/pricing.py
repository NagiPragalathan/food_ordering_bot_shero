"""Order pricing (spec step 14 and the "Pricing shown to the customer" table).

    Dish price (MRP)                  -> the menu in this database
    Delivery charge + extra fees      -> Uber Direct quote
    Coupon discount                   -> services/promotions.py
    Total                             -> dish total + Uber charges (+ our tax)
                                         - discount

Cart lines are always re-priced from the menu rather than trusting the prices
in the inbound cart payload: the message is client-supplied, and an edited or
stale cart must never be able to set what we charge.

The menu is this service's own table, managed through the admin dashboard and
imported from the client's sheet. Customers are charged the sheet's MRP; the
PPP column is stored as cost and never reaches a customer or Stripe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from app.core.config import settings
from app.core.exceptions import (ConfigurationError, IntegrationError,
                                 ItemUnavailableError)
from app.core.logging import get_logger
from app.db.models import Outlet, Promotion
from app.integrations.uber import direct as uber
from app.services import menu as menu_service
from app.services import promotions

log = get_logger(__name__)

CENTS = Decimal("0.01")
# Lead time handed to Uber when quoting; the real window comes from the slot.
DEFAULT_PICKUP_LEAD_MINUTES = 45


def money(value) -> Decimal:
    """Round to 2dp, half-up - the way money is rounded, not bankers'."""
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


@dataclass
class PricedLine:
    retailer_id: str
    name: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal

    def as_dict(self) -> dict:
        return {
            "retailer_id": self.retailer_id,
            "name": self.name,
            "quantity": self.quantity,
            "unit_price": str(self.unit_price),
            "line_total": str(self.line_total),
        }


@dataclass
class PricedOrder:
    lines: list[PricedLine] = field(default_factory=list)
    dish_total: Decimal = Decimal("0.00")
    delivery_fee: Decimal = Decimal("0.00")
    extra_fees: Decimal = Decimal("0.00")
    tax: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")
    currency: str = "USD"
    uber_quote_id: str | None = None
    uber_quote_raw: dict = field(default_factory=dict)
    delivery_quote_failed: bool = False
    # The coupon. `discount` is zero when none is applied, or when the cart
    # does not qualify yet - `promo_problem` then says why.
    discount: Decimal = Decimal("0.00")
    promotion: Promotion | None = field(default=None, repr=False)
    promo_problem: str | None = None

    @property
    def promo_code(self) -> str | None:
        """The code that took money off this order, if one did."""
        return self.promotion.code if self.promotion and self.discount > 0 else None

    @property
    def promo_title(self) -> str | None:
        return self.promotion.title if self.promotion and self.discount > 0 else None

    @property
    def total_without_coupon(self) -> Decimal:
        """What this order would cost with no coupon: a dish discount also
        lowers the tax, so this is more than total + discount when tax is on."""
        full_tax = money(self.dish_total * Decimal(str(settings.tax_percent)) / Decimal("100"))
        return money(self.dish_total + self.delivery_fee + self.extra_fees + full_tax)

    @property
    def item_count(self) -> int:
        return sum(line.quantity for line in self.lines)

    def items_as_dicts(self) -> list[dict]:
        return [line.as_dict() for line in self.lines]


async def price_cart(
    session,
    cart_items: list[dict],
    *,
    outlet: Outlet | None = None,
    dropoff_latitude: float | None = None,
    dropoff_longitude: float | None = None,
    dropoff_address: dict | None = None,
    slot_starts_at: datetime | None = None,
    promotion: Promotion | None = None,
) -> PricedOrder:
    """Price a cart end to end.

    `cart_items` is [{"retailer_id": str, "quantity": int}] as parsed from the
    WhatsApp cart message. Raises ItemUnavailableError when a line is no longer
    in the catalogue or is out of stock (spec step 8).

    `promotion` is a coupon already checked for this customer
    (promotions.resolve); whether this cart qualifies is decided here.
    """
    lines = await _price_lines(session, cart_items)
    dish_total = money(sum((line.line_total for line in lines), Decimal("0.00")))
    currency = settings.stripe_currency.upper()

    priced = PricedOrder(
        lines=lines, dish_total=dish_total, currency=currency,
    )

    # Delivery quote needs a destination and an outlet to quote from.
    if outlet is not None and dropoff_address is not None:
        quote = await _quote_delivery(
            outlet=outlet,
            dropoff_address=dropoff_address,
            dropoff_latitude=dropoff_latitude,
            dropoff_longitude=dropoff_longitude,
            slot_starts_at=slot_starts_at,
        )
        if quote is not None:
            priced.delivery_fee = money(quote.fee)
            priced.extra_fees = money(quote.extra_fees)
            priced.uber_quote_id = quote.quote_id
            priced.uber_quote_raw = quote.raw
            priced.currency = quote.currency or currency
        elif _fallback_fee() is not None:
            # Local testing without Uber credentials. Never reachable in
            # production - see `_fallback_fee`.
            priced.delivery_fee = money(_fallback_fee())
            log.warning("delivery_fee_fallback_used", fee=str(priced.delivery_fee))
        else:
            priced.delivery_quote_failed = True

    if promotion is not None:
        priced.promotion = promotion
        priced.promo_problem = promotions.amount_problem(
            promotion, dish_total=dish_total,
            delivery_fee=None if priced.delivery_quote_failed else priced.delivery_fee)
        if priced.promo_problem is None:
            priced.discount = promotions.discount_for(
                promotion, dish_total=dish_total, delivery_fee=priced.delivery_fee)

    _settle(priced)
    # Stripe cannot charge less than its minimum, so a discount that would
    # take the order under it gives a little less instead.
    if priced.discount > 0 and priced.total < promotions.MIN_CHARGE:
        priced.discount = max(Decimal("0.00"),
                              money(priced.discount - (promotions.MIN_CHARGE - priced.total)))
        _settle(priced)
    return priced


def _settle(priced: PricedOrder) -> None:
    """Tax and total from the parts. A dish discount lowers what is taxed;
    free delivery does not."""
    taxable = priced.dish_total
    if promotions.comes_off_dishes(priced.promotion):
        taxable = max(Decimal("0.00"), priced.dish_total - priced.discount)
    priced.tax = money(taxable * Decimal(str(settings.tax_percent)) / Decimal("100"))
    priced.total = money(
        priced.dish_total + priced.delivery_fee + priced.extra_fees + priced.tax
        - priced.discount
    )


def _fallback_fee() -> Decimal | None:
    """A stand-in delivery fee, or None when it must not apply.

    Guarded twice on purpose: it needs an explicit non-zero setting *and* a
    non-production environment. Charging a guessed delivery fee to a real
    customer would be worse than refusing the order.
    """
    if settings.app_env == "production":
        return None
    if not settings.delivery_fee_fallback:
        return None
    return Decimal(str(settings.delivery_fee_fallback))


async def _price_lines(session, cart_items: list[dict]) -> list[PricedLine]:
    """Look every cart line up in the menu and price it there."""
    if not cart_items:
        raise ItemUnavailableError(
            "cart is empty",
            customer_message="Your cart looks empty. Please add some dishes and send it again.",
        )

    retailer_ids = [str(item.get("retailer_id")) for item in cart_items
                    if item.get("retailer_id")]
    catalogue = await menu_service.get_by_retailer_ids(session, retailer_ids)

    lines: list[PricedLine] = []
    missing: list[str] = []

    for item in cart_items:
        retailer_id = str(item.get("retailer_id") or "")
        quantity = int(item.get("quantity") or 0)
        if not retailer_id or quantity <= 0:
            continue

        product = catalogue.get(retailer_id)
        if product is None:
            # Either unknown, or marked unavailable in the admin dashboard.
            missing.append(retailer_id)
            continue

        unit_price = money(product.price)
        lines.append(PricedLine(
            retailer_id=retailer_id,
            name=product.name,
            quantity=quantity,
            unit_price=unit_price,
            line_total=money(unit_price * quantity),
        ))

    if missing:
        # Show dish names, not slugs - the message goes to a customer.
        known = await menu_service.names_for(session, missing)
        names = ", ".join(known.get(rid, rid) for rid in missing)
        raise ItemUnavailableError(
            f"unavailable items: {names}",
            customer_message=(
                f"Sorry, these are no longer available: {names}. "
                "Please update your cart and send it again."
            ),
        )
    if not lines:
        raise ItemUnavailableError(
            "no priceable lines in cart",
            customer_message="We could not read your cart. Please add items and try again.",
        )
    return lines


async def _quote_delivery(
    *,
    outlet: Outlet,
    dropoff_address: dict,
    dropoff_latitude: float | None,
    dropoff_longitude: float | None,
    slot_starts_at: datetime | None,
) -> uber.DeliveryQuote | None:
    """Ask Uber Direct for the delivery charge.

    Returns None if Uber is unreachable. The caller decides what to do; the
    conversation handler treats it as a soft failure and asks the customer to
    retry rather than charging a guessed fee.
    """
    pickup = uber.build_address(
        street=outlet.address_line1,
        city=outlet.city,
        state=outlet.state,
        zip_code=outlet.postal_code,
        country=outlet.country or "US",
    )
    pickup_ready = slot_starts_at or (
        datetime.now(timezone.utc) + timedelta(minutes=DEFAULT_PICKUP_LEAD_MINUTES)
    )

    try:
        return await uber.uber_direct.get_quote(
            pickup=pickup,
            dropoff=dropoff_address,
            pickup_latitude=outlet.latitude,
            pickup_longitude=outlet.longitude,
            dropoff_latitude=dropoff_latitude,
            dropoff_longitude=dropoff_longitude,
            pickup_ready_at=pickup_ready,
        )
    except IntegrationError as exc:
        log.error("uber_quote_failed", outlet=outlet.code, error=str(exc))
        return None
    except ConfigurationError as exc:
        # Unconfigured is a kind of unreachable as far as the caller is
        # concerned. Raising here would surface as a 500 on the web page
        # rather than the "we could not calculate delivery" path.
        log.error("uber_not_configured", outlet=outlet.code, error=str(exc))
        return None


def format_summary(priced: PricedOrder, *, outlet_name: str,
                   slot_label: str) -> str:
    """The order summary shown before payment (spec step 14)."""
    symbol = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£"}.get(
        priced.currency.upper(), ""
    )
    lines = ["*Your order*", ""]
    for line in priced.lines:
        lines.append(f"{line.quantity} x {line.name} - {symbol}{line.line_total}")

    lines.append("")
    lines.append(f"Subtotal: {symbol}{priced.dish_total}")
    if priced.delivery_fee > 0:
        lines.append(f"Delivery: {symbol}{priced.delivery_fee}")
    if priced.extra_fees > 0:
        lines.append(f"Taxes and fees: {symbol}{priced.extra_fees}")
    if priced.tax > 0:
        lines.append(f"Tax: {symbol}{priced.tax}")
    if priced.discount > 0:
        lines.append(f"Coupon {priced.promo_code}: -{symbol}{priced.discount}")
    lines.append(f"*Total: {symbol}{priced.total}*")
    if priced.discount > 0:
        lines.append(f"You save {symbol}{priced.discount} on this order!")
    lines.append("")
    lines.append(f"From: {outlet_name}")
    lines.append(f"Delivery slot: {slot_label}")
    return "\n".join(lines)
