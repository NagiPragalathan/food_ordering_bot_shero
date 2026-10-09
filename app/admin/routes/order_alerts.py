"""Order alerts: the WhatsApp numbers told about every paid order.

The card sits on the Settings page; this saves it (ORDER_ALERT_NUMBERS) and
sends a test alert. The sending itself is services/order_alerts.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import redirect, require_admin
from app.core.logging import get_logger
from app.db.models import AdminUser
from app.db.session import get_session
from app.integrations.gallabox import templates as tpl
from app.integrations.gallabox.sender import current_sender
from app.services import allowlist, order_alerts
from app.services.settings_store import save_many

log = get_logger(__name__)
router = APIRouter(prefix="/order-alerts", tags=["admin"])

# What the test alert says, so nobody mistakes it for a real order.
TEST_VALUES = ("TEST - not a real order", "Test Customer, +1 443 555 0142", "0.00",
               "Tomorrow, 7:00 PM - 8:00 PM", "Shero Kitchen",
               "6360 Lawyers Hill Road, 21075", "1 x Test Dish")


def _back(request: Request) -> str:
    return str(request.url_for("admin_settings")) + "#order-alerts"


@router.post("", name="admin_order_alerts_save")
async def save_order_alerts(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    form = await request.form()
    rows = [(str(n).strip(), str(name)) for n, name
            in zip(form.getlist("number"), form.getlist("name")) if str(n).strip()]
    invalid = [n for n, _ in rows if not allowlist.is_valid(n)]
    if invalid:
        return redirect(_back(request), flash=("error", "Not a full WhatsApp number (10 to 15 "
                                                        "digits, with country code): "
                                                        + ", ".join(invalid)))
    listed = allowlist.parse_entries(",".join(
        f"{n}|{allowlist.clean_name(name)}" for n, name in rows))
    await save_many(session, {"ORDER_ALERT_NUMBERS": allowlist.format_entries(listed)},
                    updated_by=current_user.email, allow_blank={"ORDER_ALERT_NUMBERS"})
    log.info("order_alerts_saved", numbers=len(listed), by=current_user.email)
    if not listed:
        return redirect(_back(request), flash=("success", "Order alerts are off: no numbers."))
    return redirect(_back(request), flash=("success", f"Every paid order will now be sent to "
                                                      f"{len(listed)} number(s) on WhatsApp."))


@router.post("/test", name="admin_order_alerts_test")
async def send_test_alert(
    request: Request,
    current_user: AdminUser = Depends(require_admin),
):
    entries = order_alerts.entries()
    if not entries:
        return redirect(_back(request), flash=("error", "Add a number and save it first."))
    failed = []
    for entry in entries:
        try:
            await current_sender().send_template(entry.number, tpl.ORDER_ALERT, *TEST_VALUES)
        except Exception as exc:  # noqa: BLE001 - report it on the page
            log.error("order_alert_test_failed", number_tail=entry.number[-4:], error=str(exc))
            failed.append(entry.name or entry.number)
    log.info("order_alert_test", numbers=len(entries), failed=len(failed),
             by=current_user.email)
    if failed:
        return redirect(_back(request), flash=("error", "The test alert could not be sent to: "
                                                        + ", ".join(failed)))
    return redirect(_back(request), flash=("success", f"Test alert sent to {len(entries)} "
                                                      "number(s). Check WhatsApp."))
