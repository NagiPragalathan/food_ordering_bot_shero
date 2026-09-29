"""Settings: connect Zoho, WhatsApp, Stripe, Uber and Meta from the browser.

Values saved here override the environment, so credentials can be rotated
without a redeploy. Secrets are encrypted at rest and never rendered back to
the page - a blank secret field means "leave it as it is".

Each integration has a Test button that makes one real, read-only call, so a
wrong key is found here rather than mid-order.
"""

from __future__ import annotations

import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import redirect, render, require_admin
from app.core.config import is_unset, settings
from app.core.exceptions import IntegrationError
from app.core.logging import get_logger
from app.db.models import AdminUser, Outlet
from app.db.session import get_session
from app.integrations.zoho import oauth as zoho_oauth
from app.services import connection_tests, zoho_connect
# Same key order slot generation reads, so the two cannot drift apart.
from app.services.slots import WEEKDAY_KEYS
from app.services.settings_store import (
    EDITABLE_KEYS,
    SETTING_GROUPS,
    SecretsUnavailable,
    save_many,
    stored_keys,
)

log = get_logger(__name__)
router = APIRouter(prefix="/settings", tags=["admin"])


@router.get("", name="admin_settings")
async def settings_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    overridden = await stored_keys(session)

    groups = []
    for title, fields in SETTING_GROUPS.items():
        rendered = []
        for key, label, is_secret in fields:
            current = getattr(settings, key.lower(), "")
            rendered.append({
                "key": key,
                "label": label,
                "is_secret": is_secret,
                # Secrets are never sent to the browser - only whether one is set.
                "value": "" if is_secret else str(current or ""),
                "is_set": not is_unset(str(current or "")),
                "from_database": key in overridden,
            })
        groups.append({"title": title, "fields": rendered})

    kitchen = (await session.execute(
        select(Outlet).order_by(Outlet.is_primary.desc(), Outlet.created_at)
    )).scalars().first()

    return render(request, "admin/settings.html", {
        "current_user": current_user,
        "groups": groups,
        "kitchen": kitchen,
        "secrets_enabled": not is_unset(settings.settings_encryption_key),
        "missing_credentials": settings.missing_credentials(),
        # Zoho is connected by OAuth rather than by pasting a refresh token.
        "zoho": {
            # The client id and secret live in .env; the card is one button.
            "client_ready": not (is_unset(settings.zoho_client_id)
                                 or is_unset(settings.zoho_client_secret)),
            "connected": not is_unset(settings.zoho_refresh_token),
            "centres": zoho_connect.DATA_CENTRES,
            "data_centre": settings.zoho_data_center or "com",
        },
    })


@router.post("", name="admin_settings_save")
async def save_settings(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    form = await request.form()
    values = {
        key: str(form.get(key, ""))
        for key in EDITABLE_KEYS
        if key in form
    }

    try:
        changed = await save_many(session, values, updated_by=current_user.email)
    except SecretsUnavailable as exc:
        return redirect(str(request.url_for("admin_settings")),
                        flash=("error", str(exc)))

    url = str(request.url_for("admin_settings"))
    if not changed:
        return redirect(url, flash=("success", "No changes to save."))

    log.info("settings_updated", keys=changed, by=current_user.email)
    return redirect(url, flash=(
        "success",
        f"Saved {len(changed)} setting(s): {', '.join(changed)}. "
        "Changes are live on this instance now.",
    ))


@router.post("/zoho/connect", name="admin_zoho_connect")
async def zoho_start(
    request: Request,
    zoho_data_center: str = Form(default="com"),
    current_user: AdminUser = Depends(require_admin),
):
    """Hand the admin to Zoho's consent screen, on the chosen domain.

    The callback prefers the data centre Zoho reports, and falls back to the
    one chosen here (carried in a cookie with the state).
    """
    url = str(request.url_for("admin_settings"))
    centre = zoho_data_center.strip().lower()
    if centre not in dict(zoho_connect.DATA_CENTRES):
        return redirect(url, flash=("error", f"Unknown Zoho domain '{centre}'."))
    if is_unset(settings.zoho_client_id) or is_unset(settings.zoho_client_secret):
        return redirect(url, flash=(
            "error", "Add ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET to .env first."))

    # Carried through Zoho and checked on the way back, so a callback that did
    # not originate here is rejected.
    state = zoho_connect.new_state()
    log.info("zoho_connect_started", centre=centre, by=current_user.email)

    response = RedirectResponse(
        zoho_connect.authorize_url(settings.zoho_client_id.strip(), centre, state),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.set_cookie("zoho_oauth_state", state, max_age=900, httponly=True,
                        samesite="lax")
    response.set_cookie("zoho_oauth_centre", centre, max_age=900, httponly=True,
                        samesite="lax")
    return response


@router.get("/zoho/callback", name="admin_zoho_callback")
async def zoho_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Zoho sends the grant token here; swap it for a refresh token."""
    url = str(request.url_for("admin_settings"))

    if error:
        return redirect(url, flash=("error", f"Zoho returned '{error}'."))

    expected = request.cookies.get("zoho_oauth_state")
    if not expected or not secrets.compare_digest(state, expected):
        log.warning("zoho_callback_bad_state", by=current_user.email)
        return redirect(url, flash=(
            "error", "That Zoho response did not match this browser session. "
                     "Press Connect Zoho again."))

    if not code:
        return redirect(url, flash=("error", "Zoho sent no authorisation code."))

    # Where the account lives (us, in, eu, ...). The code can only be
    # exchanged there, and every later API call goes there too.
    location = request.query_params.get("location")
    centre = zoho_connect.centre_from_location(location)
    if centre is None:
        if location:
            log.warning("zoho_callback_unknown_location", location=location)
        chosen = request.cookies.get("zoho_oauth_centre", "")
        centre = chosen if chosen in dict(zoho_connect.DATA_CENTRES) \
            else (settings.zoho_data_center or "com")

    try:
        refresh_token = await zoho_connect.exchange_code(
            code, settings.zoho_client_id, settings.zoho_client_secret, centre,
        )
        await save_many(session, {"ZOHO_REFRESH_TOKEN": refresh_token,
                                  "ZOHO_DATA_CENTER": centre},
                        updated_by=current_user.email)
    except IntegrationError as exc:
        log.error("zoho_connect_failed", error=exc.message)
        return redirect(url, flash=("error", exc.message))
    except SecretsUnavailable:
        return redirect(url, flash=(
            "error", "Set SETTINGS_ENCRYPTION_KEY before storing the token."))

    # A token cached from before (another account or data centre) is stale.
    zoho_oauth.invalidate_token()
    org_id, dropped = await zoho_connect.record_org(session, updated_by=current_user.email)
    log.info("zoho_connected", centre=centre, org=org_id, by=current_user.email)
    message = f"Zoho connected (data centre: zoho.{centre})."
    if dropped:
        message += (f" This is a different Zoho org from before, so the links "
                    f"{dropped} customer(s) had to the old CRM were dropped. "
                    "Push to Zoho on the Customers page recreates them.")
    response = redirect(url, flash=("success", message))
    response.delete_cookie("zoho_oauth_state")
    response.delete_cookie("zoho_oauth_centre")
    return response


@router.post("/zoho/disconnect", name="admin_zoho_disconnect")
async def zoho_disconnect(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Forget the stored refresh token. The client id and secret stay."""
    url = str(request.url_for("admin_settings"))
    await save_many(session, {"ZOHO_REFRESH_TOKEN": ""},
                    updated_by=current_user.email, allow_blank={"ZOHO_REFRESH_TOKEN"})
    log.info("zoho_disconnected", by=current_user.email)
    return redirect(url, flash=("success", "Zoho disconnected."))


@router.post("/test/{service}", name="admin_settings_test")
async def test_connection(
    request: Request,
    service: str,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Make one real read-only call to prove the credentials work."""
    url = str(request.url_for("admin_settings"))

    tester = connection_tests.TESTS.get(service)
    if tester is None:
        return redirect(url, flash=("error", f"Unknown service '{service}'."))

    result = await tester()
    log.info("connection_test", service=service, ok=result.ok,
             by=current_user.email)
    return redirect(url, flash=(
        "success" if result.ok else "error",
        f"{result.service}: {result.message}",
    ))


@router.post("/kitchen", name="admin_kitchen_save")
async def save_kitchen(
    request: Request,
    name: str = Form(...),
    address_line1: str = Form(...),
    city: str = Form(...),
    state: str = Form(...),
    postal_code: str = Form(...),
    latitude: float = Form(...),
    longitude: float = Form(...),
    service_area_mode: str = Form(default="radius"),
    delivery_radius_km: float = Form(default=10.0),
    service_zips: str = Form(default=""),
    kitchen_whatsapp: str = Form(default=""),
    timezone_name: str = Form(default="America/New_York"),
    slot_length_minutes: int = Form(default=60),
    slot_capacity: int = Form(default=4),
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Create or update the single kitchen and its delivery area."""
    url = str(request.url_for("admin_settings"))
    # Seven day pairs is too many to declare as Form parameters, so the
    # opening hours are read straight off the submitted form.
    operating_hours = _parse_hours(await request.form())

    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return redirect(url, flash=("error", "Latitude or longitude is out of range."))

    kitchen = (await session.execute(
        select(Outlet).order_by(Outlet.is_primary.desc(), Outlet.created_at)
    )).scalars().first()

    if kitchen is None:
        kitchen = Outlet(code="MAIN", name=name, address_line1=address_line1,
                         city=city, state=state, postal_code=postal_code,
                         latitude=latitude, longitude=longitude)
        session.add(kitchen)

    kitchen.name = name
    kitchen.address_line1 = address_line1
    kitchen.city = city
    kitchen.state = state
    kitchen.postal_code = postal_code
    kitchen.latitude = latitude
    kitchen.longitude = longitude
    kitchen.service_area_mode = service_area_mode
    kitchen.delivery_radius_km = delivery_radius_km
    kitchen.service_zips = _parse_zips(service_zips)
    kitchen.kitchen_whatsapp = kitchen_whatsapp.strip() or None
    kitchen.timezone = timezone_name.strip() or "America/New_York"
    kitchen.operating_hours = operating_hours
    kitchen.slot_length_minutes = max(15, min(240, slot_length_minutes))
    kitchen.slot_capacity = max(1, slot_capacity)
    kitchen.is_primary = True
    kitchen.is_active = True

    await session.flush()
    log.info("kitchen_saved", name=name, mode=service_area_mode,
             zips=len(kitchen.service_zips), open_days=len(operating_hours),
             by=current_user.email)

    if not operating_hours:
        # Slots are generated only inside opening hours, so a kitchen with
        # none set looks fine here and then fails at the slot step.
        return redirect(url, flash=(
            "error",
            f"Saved kitchen '{name}', but no opening hours are set - "
            "customers will be told there are no delivery slots. "
            "Set at least one day below.",
        ))
    return redirect(url, flash=("success", f"Saved kitchen '{name}'."))


def _parse_hours(form) -> dict[str, list[list[str]]]:
    """Build the `operating_hours` blob from the seven open/close pairs.

    Shape matches what `slots._windows_for_day` reads:
    `{"mon": [["11:00", "21:00"]], ...}`. A day with either field blank is
    treated as closed and simply left out, which is how the kitchen says
    "we do not deliver on Sundays".

    A close time at or before the open time is kept as-is: slot generation
    reads that as trading past midnight, which is a real case for a kitchen
    open 18:00-01:00.
    """
    hours: dict[str, list[list[str]]] = {}
    for key in WEEKDAY_KEYS:
        opens = _clean_time(form.get(f"hours_{key}_open"))
        closes = _clean_time(form.get(f"hours_{key}_close"))
        if opens is None or closes is None:
            if opens != closes:  # one side filled in, the other not
                log.warning("kitchen_hours_ignored", day=key,
                            opens=form.get(f"hours_{key}_open"),
                            closes=form.get(f"hours_{key}_close"))
            continue
        hours[key] = [[opens, closes]]
    return hours


def _clean_time(raw) -> str | None:
    """Normalise one time field to `HH:MM`, or None if it is not a time.

    Browsers post `<input type="time">` as HH:MM, but some send HH:MM:SS, and
    the endpoint is reachable by anything. Storing the submitted string
    verbatim would put whatever arrived into the JSON blob that slot
    generation later re-parses, so canonicalise here instead.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time().strftime("%H:%M")
        except ValueError:
            continue
    return None


def _parse_zips(raw: str) -> list[str]:
    """Split a comma/newline separated ZIP list into normalised codes."""
    parts = raw.replace("\n", ",").replace(";", ",").split(",")
    seen: dict[str, None] = {}
    for part in parts:
        code = "".join(part.upper().split()).replace("-", "")
        if code:
            seen.setdefault(code, None)
    return list(seen)
