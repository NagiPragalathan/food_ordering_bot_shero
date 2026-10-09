"""Delivery slots (spec step 13).

Slots are generated from each outlet's operating hours - the spec requires
that "only slots within operating hours are returned" - then persisted so
capacity can be tracked.

A slot is *held* the moment the customer picks it and only becomes *booked*
when Stripe confirms payment. Holds expire with the payment link, which is
what frees the window again for somebody else.

Concurrency: reservation uses a single conditional UPDATE
(`reserved_count < capacity`) so two customers paying simultaneously can never
oversell the same window. A read-then-write in Python would race.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import IntegrationError, NoSlotsAvailableError
from app.core.logging import get_logger
from app.db.models import DeliverySlot, Order, Outlet, SlotHold, SlotHoldStatus

log = get_logger(__name__)

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# How many days of slots to offer, counted from the earliest bookable time.
DEFAULT_DAYS_AHEAD = 3


def earliest_bookable(now: datetime | None = None) -> datetime:
    """The first moment a delivery slot may start: SLOT_MIN_LEAD_HOURS after
    now (24 by default - the food is cooked to order)."""
    return (now or datetime.now(timezone.utc)) + timedelta(hours=settings.slot_min_lead_hours)


def _lead_days() -> int:
    """Whole days the lead time spans, so generation reaches far enough."""
    return math.ceil(settings.slot_min_lead_hours / 24)


def as_utc(value: datetime) -> datetime:
    """Normalise a datetime read back from the database to UTC-aware.

    Postgres returns timezone-aware values for TIMESTAMPTZ columns, but not
    every driver does (SQLite, used in tests, returns naive ones). A naive
    value silently breaks every comparison against `datetime.now(timezone.utc)`,
    so it is pinned to UTC here rather than trusted.
    """
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class SlotOption:
    slot_id: str
    label: str
    starts_at: datetime
    ends_at: datetime
    remaining: int


# --- public API --------------------------------------------------------------
async def list_available_slots(
    session: AsyncSession,
    outlet: Outlet,
    *,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    limit: int = 10,
) -> list[SlotOption]:
    """Bookable slots for an outlet, soonest first.

    The spec's "No slots today -> show next available day" falls out
    naturally: the search runs forward across `days_ahead` days and returns
    whatever it finds first.
    """
    if settings.slot_source == "remote":
        return await _list_slots_remote(outlet, days_ahead=days_ahead, limit=limit)

    # The window starts at the earliest bookable time, so a 24-hour lead
    # still leaves `days_ahead` days of choice rather than eating one.
    await ensure_slots(session, outlet, days_ahead=days_ahead + _lead_days())

    earliest = earliest_bookable()
    horizon = earliest + timedelta(days=days_ahead)

    result = await session.execute(
        select(DeliverySlot)
        .where(
            DeliverySlot.outlet_id == outlet.id,
            DeliverySlot.is_active.is_(True),
            DeliverySlot.starts_at >= earliest,
            DeliverySlot.starts_at <= horizon,
            DeliverySlot.reserved_count < DeliverySlot.capacity,
        )
        .order_by(DeliverySlot.starts_at)
        .limit(limit)
    )
    tz = _outlet_tz(outlet)
    return [
        SlotOption(
            slot_id=str(slot.id),
            label=slot.label(tz),
            starts_at=as_utc(slot.starts_at),
            ends_at=as_utc(slot.ends_at),
            remaining=slot.remaining,
        )
        for slot in result.scalars()
    ]


async def hold_slot(
    session: AsyncSession,
    *,
    slot_id: str | uuid.UUID,
    order_id: uuid.UUID,
    ttl_minutes: int | None = None,
) -> SlotHold:
    """Reserve a slot while payment is pending.

    Raises NoSlotsAvailableError if the slot filled up between being shown and
    being picked, which the handler turns into "please choose another slot".
    """
    slot_uuid = uuid.UUID(str(slot_id))
    ttl = ttl_minutes or settings.slot_hold_minutes

    # Atomic claim: only succeeds while spare capacity remains.
    claimed = await session.execute(
        update(DeliverySlot)
        .where(
            DeliverySlot.id == slot_uuid,
            DeliverySlot.is_active.is_(True),
            DeliverySlot.reserved_count < DeliverySlot.capacity,
        )
        .values(reserved_count=DeliverySlot.reserved_count + 1)
        .returning(DeliverySlot.id)
    )
    if claimed.scalar_one_or_none() is None:
        raise NoSlotsAvailableError(
            f"slot {slot_uuid} is full",
            customer_message=(
                "Sorry, that delivery slot was just taken. Please pick another one."
            ),
        )

    # Only once the new slot is secured: drop any earlier hold for this order,
    # so switching slots does not leak capacity on the window the customer
    # moved away from. Doing this after the claim means a failed claim leaves
    # their existing reservation intact.
    await release_holds_for_order(session, order_id, reason="reselected")

    hold = SlotHold(
        slot_id=slot_uuid,
        order_id=order_id,
        status=SlotHoldStatus.HELD,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=ttl),
    )
    session.add(hold)
    await session.flush()
    log.info("slot_held", slot_id=str(slot_uuid), order_id=str(order_id), ttl=ttl)
    return hold


async def book_slot(session: AsyncSession, order_id: uuid.UUID) -> bool:
    """Promote this order's hold to a booking (spec step 16)."""
    result = await session.execute(
        select(SlotHold).where(
            SlotHold.order_id == order_id,
            SlotHold.status == SlotHoldStatus.HELD,
        )
    )
    hold = result.scalar_one_or_none()
    if hold is None:
        log.warning("slot_book_no_hold", order_id=str(order_id))
        return False

    hold.status = SlotHoldStatus.BOOKED
    hold.expires_at = None
    # Flush here rather than relying on the caller: the booking must be
    # durable before the confirmation message goes out.
    await session.flush()
    log.info("slot_booked", slot_id=str(hold.slot_id), order_id=str(order_id))
    return True


async def release_holds_for_order(session: AsyncSession, order_id: uuid.UUID, *,
                                  reason: str = "expired",
                                  include_booked: bool = False) -> int:
    """Release any active hold for an order and give the capacity back.

    `include_booked` also frees a paid booking - only for deleting the order
    outright (admin Customers page), never for a live paid order.
    """
    active = [SlotHoldStatus.HELD]
    if include_booked:
        active.append(SlotHoldStatus.BOOKED)
    result = await session.execute(
        select(SlotHold).where(
            SlotHold.order_id == order_id,
            SlotHold.status.in_([str(s) for s in active]),
        )
    )
    holds = list(result.scalars())
    for hold in holds:
        hold.status = SlotHoldStatus.RELEASED
        hold.released_at = datetime.now(timezone.utc)
        # Floor at zero: a manual DB fix-up must never push the counter negative.
        await session.execute(
            update(DeliverySlot)
            .where(DeliverySlot.id == hold.slot_id, DeliverySlot.reserved_count > 0)
            .values(reserved_count=DeliverySlot.reserved_count - 1)
        )
        log.info("slot_released", slot_id=str(hold.slot_id),
                 order_id=str(order_id), reason=reason)
    return len(holds)


async def get_slot(session: AsyncSession,
                   slot_id: str | uuid.UUID) -> DeliverySlot | None:
    return await session.get(DeliverySlot, uuid.UUID(str(slot_id)))


# --- slot generation ---------------------------------------------------------
async def ensure_slots(session: AsyncSession, outlet: Outlet, *,
                       days_ahead: int = DEFAULT_DAYS_AHEAD) -> int:
    """Create any missing slots for the next `days_ahead` days.

    Idempotent: existing start times are skipped, so this is safe to call on
    every slot listing.
    """
    tz = _outlet_tz(outlet)
    today = datetime.now(tz).date()

    existing_result = await session.execute(
        select(DeliverySlot.starts_at).where(
            DeliverySlot.outlet_id == outlet.id,
            DeliverySlot.starts_at >= datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    existing = {as_utc(dt).astimezone(timezone.utc)
                for dt in existing_result.scalars()}

    created = 0
    for offset in range(days_ahead + 1):
        day = today + timedelta(days=offset)
        for start, end in _windows_for_day(outlet, day, tz):
            if start.astimezone(timezone.utc) in existing:
                continue
            session.add(DeliverySlot(
                outlet_id=outlet.id,
                starts_at=start.astimezone(timezone.utc),
                ends_at=end.astimezone(timezone.utc),
                capacity=outlet.slot_capacity,
                reserved_count=0,
            ))
            created += 1

    if created:
        await session.flush()
        log.info("slots_generated", outlet=outlet.code, created=created)
    return created


async def rebuild_future(session: AsyncSession, outlet: Outlet) -> int:
    """After a kitchen is edited: drop future slots that no longer fit it.

    ensure_slots only adds, so slots made under the old opening hours, slot
    length or timezone would otherwise stay on offer. A slot is removed when
    it no longer matches the kitchen's windows and nobody holds or ordered
    it; a booked slot always stays. Kept free slots take the new capacity.
    Returns how many were removed.
    """
    tz = _outlet_tz(outlet)
    now = datetime.now(timezone.utc)
    future = list((await session.execute(
        select(DeliverySlot).where(DeliverySlot.outlet_id == outlet.id,
                                   DeliverySlot.starts_at > now)
    )).scalars())
    if not future:
        return 0
    ids = [s.id for s in future]
    taken = set((await session.execute(
        select(SlotHold.slot_id).where(SlotHold.slot_id.in_(ids)))).scalars())
    taken |= set((await session.execute(
        select(Order.slot_id).where(Order.slot_id.in_(ids)))).scalars())

    windows: dict = {}

    def wanted(day) -> set[tuple[datetime, datetime]]:
        if day not in windows:
            windows[day] = {(a.astimezone(timezone.utc), b.astimezone(timezone.utc))
                            for a, b in _windows_for_day(outlet, day, tz)}
        return windows[day]

    removed = 0
    for slot in future:
        start, end = as_utc(slot.starts_at), as_utc(slot.ends_at)
        local_day = start.astimezone(tz).date()
        # The day before too: a window that runs past midnight.
        fits = (start, end) in wanted(local_day) | wanted(local_day - timedelta(days=1))
        if slot.id in taken or slot.reserved_count:
            continue
        if fits:
            slot.capacity = outlet.slot_capacity
            continue
        await session.delete(slot)
        removed += 1
    if removed:
        await session.flush()
        log.info("slots_rebuilt", outlet=outlet.code, removed=removed)
    return removed


def _windows_for_day(outlet: Outlet, day, tz) -> list[tuple[datetime, datetime]]:
    """Split one day of operating hours into fixed-length delivery windows."""
    key = WEEKDAY_KEYS[day.weekday()]
    ranges = (outlet.operating_hours or {}).get(key) or []
    length = timedelta(minutes=outlet.slot_length_minutes or 60)

    windows: list[tuple[datetime, datetime]] = []
    for entry in ranges:
        try:
            open_at, close_at = _parse_range(entry)
        except (ValueError, TypeError, IndexError):
            log.warning("bad_operating_hours", outlet=outlet.code, day=key,
                        entry=repr(entry))
            continue

        cursor = datetime.combine(day, open_at, tzinfo=tz)
        closing = datetime.combine(day, close_at, tzinfo=tz)
        # A close time at or before the open time means it runs past midnight.
        if closing <= cursor:
            closing += timedelta(days=1)

        while cursor + length <= closing:
            windows.append((cursor, cursor + length))
            cursor += length
    return windows


def _parse_range(entry) -> tuple[time, time]:
    return time.fromisoformat(entry[0]), time.fromisoformat(entry[1])


def outlet_tz(outlet: Outlet):
    """Public alias - callers outside this module need it to label slots."""
    return _outlet_tz(outlet)


def _outlet_tz(outlet: Outlet):
    try:
        return ZoneInfo(outlet.timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown_outlet_timezone", outlet=outlet.code, tz=outlet.timezone)
        return timezone.utc


# --- remote source -----------------------------------------------------------
async def _list_slots_remote(outlet: Outlet, *, days_ahead: int,
                             limit: int) -> list[SlotOption]:
    """Call the client's slots API.

    Expected response (to confirm with the client):
        {"slots": [{"id", "label", "starts_at", "ends_at", "remaining"}]}
    """
    from app.integrations.client_backend import client_backend

    if not settings.client_backend_base_url:
        raise IntegrationError(
            "client_backend",
            "SLOT_SOURCE=remote but CLIENT_BACKEND_BASE_URL is not set",
        )

    payload = await client_backend.get(
        "/outlets/slots",
        params={"outlet_id": outlet.code or str(outlet.id), "days": days_ahead},
    )
    rows = (payload or {}).get("slots", []) if isinstance(payload, dict) else (payload or [])

    options: list[SlotOption] = []
    earliest = earliest_bookable()
    for row in rows:
        if len(options) >= limit:
            break
        starts = _parse_iso(row.get("starts_at"))
        ends = _parse_iso(row.get("ends_at"))
        # The lead time holds whatever the backend offers.
        if not starts or not ends or as_utc(starts) < earliest:
            continue
        options.append(SlotOption(
            slot_id=str(row.get("id")),
            label=row.get("label") or f"{starts:%a %d %b, %I:%M %p}",
            starts_at=starts,
            ends_at=ends,
            remaining=int(row.get("remaining", 1)),
        ))
    return options


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
