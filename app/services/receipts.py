"""The bill a customer can download once they have paid (spec step 16).

The payment confirmation carries a link to `/receipt/<token>`. The token is
the order number signed with the app secret, so the URL cannot be guessed or
edited to open somebody else's bill, and it never expires: a customer may
want the bill weeks later. Only a paid order has a bill.

The PDF is drawn with fpdf2 from the order snapshot, so it shows exactly what
was charged even if menu prices change later.
"""

from __future__ import annotations

from datetime import timezone
from decimal import Decimal
from pathlib import Path

from fpdf import FPDF
from itsdangerous import BadSignature, URLSafeSerializer

from app.core.config import settings
from app.db.models import Customer, Order, Outlet
from app.integrations.zoho import crm
from app.integrations.zoho import fields as f

SALT = "shero-receipt"
LOGO = Path(__file__).resolve().parents[1] / "static" / "brand" / "shero-logo-192.png"
BRAND = (15, 94, 88)        # the admin teal, as RGB
MUTED = (110, 110, 110)


def _serializer() -> URLSafeSerializer:
    secret = settings.admin_session_secret or settings.settings_encryption_key
    if not secret:
        raise RuntimeError("ADMIN_SESSION_SECRET is required to sign receipt links.")
    return URLSafeSerializer(secret, salt=SALT)


def build_token(order_number: str) -> str:
    return _serializer().dumps(order_number)


def read_token(token: str) -> str | None:
    """The order number in a receipt token, or None if it was tampered with."""
    try:
        value = _serializer().loads(token)
    except BadSignature:
        return None
    return value if isinstance(value, str) else None


def receipt_url(order_number: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/receipt/{build_token(order_number)}"


def filename(order: Order) -> str:
    return f"Shero-bill-{order.order_number}.pdf"


def build_pdf(order: Order, customer: Customer, outlet: Outlet | None) -> bytes:
    """The bill as PDF bytes."""
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(18, 18, 18)
    pdf.add_page()
    currency = "$" if (order.currency or "USD").upper() == "USD" else f"{order.currency} "

    # Header: logo, business, and the bill's own details on the right.
    if LOGO.exists():
        pdf.image(str(LOGO), x=18, y=16, w=18)
    pdf.set_xy(40, 17)
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(*BRAND)
    pdf.cell(90, 8, _t(outlet.name if outlet else "Shero Home Food"))
    pdf.set_xy(40, 25)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*MUTED)
    pdf.cell(90, 5, _t(_outlet_address(outlet)))

    pdf.set_xy(130, 17)
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(62, 8, "PAID BILL", align="R")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*MUTED)
    for offset, line in enumerate((f"Order #{order.order_number}",
                                   f"Paid {_when(order)}")):
        pdf.set_xy(130, 25 + offset * 5)
        pdf.cell(62, 5, _t(line), align="R")

    # Who and where.
    pdf.set_xy(18, 44)
    pdf.set_draw_color(220, 220, 220)
    pdf.line(18, 42, 192, 42)
    _label(pdf, "Billed to")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(20, 20, 20)
    for line in (customer.name or "Customer",
                 crm.phone(order.contact_number or customer.whatsapp_number),
                 customer.email or ""):
        if line:
            pdf.cell(0, 5, _t(line), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    _label(pdf, "Delivery")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(20, 20, 20)
    address = ", ".join(p for p in (order.delivery_address, order.apartment_unit,
                                    order.postal_code) if p)
    pdf.multi_cell(0, 5, _t(address or "-"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, _t(order.slot_label or ""), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # Items.
    widths = (96, 20, 29, 29)
    pdf.set_fill_color(*BRAND)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 9)
    for width, head, align in zip(widths, ("Item", "Qty", "Price", "Amount"),
                                  ("L", "C", "R", "R")):
        pdf.cell(width, 8, head, fill=True, align=align)
    pdf.ln()
    pdf.set_text_color(20, 20, 20)
    pdf.set_font("Helvetica", "", 9)
    for line in order.items or []:
        parsed = f.parse_line(line)
        if parsed is None:
            continue
        _, name, quantity, unit_price = parsed
        pdf.cell(widths[0], 7, _t(_fit(pdf, name, widths[0] - 2)))
        pdf.cell(widths[1], 7, str(quantity), align="C")
        pdf.cell(widths[2], 7, _money(currency, unit_price), align="R")
        pdf.cell(widths[3], 7, _money(currency, unit_price * quantity), align="R",
                 new_x="LMARGIN", new_y="NEXT")
    pdf.line(18, pdf.get_y() + 1, 192, pdf.get_y() + 1)
    pdf.ln(4)

    # Totals.
    rows = [("Dish total", order.dish_total), ("Delivery charge", order.delivery_fee)]
    if order.extra_fees:
        rows.append(("Taxes and fees", order.extra_fees))
    if order.tax:
        rows.append(("Tax", order.tax))
    for label, amount in rows:
        pdf.set_x(110)
        pdf.cell(53, 6, label)
        pdf.cell(29, 6, _money(currency, amount), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(110)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(53, 9, "Total paid")
    pdf.cell(29, 9, _money(currency, order.total), align="R", new_x="LMARGIN", new_y="NEXT")

    # Footer.
    pdf.ln(8)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*MUTED)
    if order.stripe_payment_intent_id:
        pdf.cell(0, 4, _t(f"Payment reference: {order.stripe_payment_intent_id}"),
                 new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 4, "Paid online by card through Stripe. Thank you for ordering with Shero!",
             new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


# --- helpers -----------------------------------------------------------------
def _label(pdf: FPDF, text: str) -> None:
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*MUTED)
    pdf.cell(0, 5, text.upper(), new_x="LMARGIN", new_y="NEXT")


def _money(currency: str, amount) -> str:
    return f"{currency}{Decimal(str(amount or 0)):.2f}"


def _when(order: Order) -> str:
    stamp = order.paid_at or order.created_at
    if stamp is None:
        return "-"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def _outlet_address(outlet: Outlet | None) -> str:
    if outlet is None:
        return ""
    return ", ".join(p for p in (outlet.address_line1, outlet.city, outlet.state,
                                 outlet.postal_code) if p)


def _fit(pdf: FPDF, text: str, width: float) -> str:
    """Shorten a long dish name to fit its column, with an ellipsis."""
    text = _t(text)
    if pdf.get_string_width(text) <= width:
        return text
    while text and pdf.get_string_width(text + "...") > width:
        text = text[:-1]
    return text.rstrip() + "..."


def _t(text: str) -> str:
    """The built-in PDF fonts are Latin-1; replace anything outside it."""
    return str(text).encode("latin-1", "replace").decode("latin-1")
