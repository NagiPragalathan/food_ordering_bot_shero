"""The enrollment receipt: a PDF the student gets on WhatsApp once they have paid.

    GET /sft/receipt/<pay token>/SFT-Enrollment-Receipt-<number>.pdf

The lead's random pay token is the key (the same one their Pay link carries),
so a receipt link opens that one student's receipt and nothing else; the file
name at the end is what WhatsApp shows. Only a paid lead has a receipt.

Drawn with fpdf2, as the food bills are (app/services/receipts.py), from what
was recorded at payment: the amount paid, the offer used, the payment
reference. A test payment says so in large letters: nobody was charged.
"""

from __future__ import annotations

import re
from datetime import timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from fpdf import FPDF

from app.core.config import settings
from app.db.models import SftLead
from app.services import media
from app.sft.config import SftConfig

TOKEN = re.compile(r"[A-Za-z0-9_-]{8,64}")
NAME = re.compile(r"SFT-Enrollment-Receipt-SFT-[0-9A-Z-]{6,40}\.pdf")

LOGO = Path(__file__).resolve().parents[1] / "static" / "brand" / "shero-logo-192.png"
BRAND = (2, 107, 105)        # brand-700, the logo teal a shade darker for print
BRAND_LIGHT = (230, 245, 245)
INK = (13, 31, 30)
MUTED = (100, 112, 112)
SAVING = (31, 122, 69)
WARN = (180, 83, 9)

COURSE = "Shero Food Training (SFT)"
COURSE_LINES = ("5-day self-paced online course",
                "10 short video lessons (52 minutes) and 10 quizzes",
                "Ingredients, storage, base preparation, hygiene, packing, labels,",
                "allergens, order execution and the customer handoff")


def number(lead: SftLead) -> str:
    """The receipt number: SFT-<date paid>-<six of the lead's id>."""
    stamp = lead.paid_at.strftime("%Y%m%d") if lead.paid_at else "PENDING"
    return f"SFT-{stamp}-{lead.id.hex[:6].upper()}"


def filename(lead: SftLead) -> str:
    return f"SFT-Enrollment-Receipt-{number(lead)}.pdf"


def url(lead: SftLead) -> str:
    base = settings.public_base_url.rstrip("/")
    return f"{base}/sft/receipt/{lead.pay_token}/{filename(lead)}"


def _folder(token: str) -> Path | None:
    if not TOKEN.fullmatch(token or ""):
        return None
    return media.MEDIA_DIR / "sft" / "receipts" / token


def save(lead: SftLead, cfg: SftConfig, *, offer_title: str | None = None) -> Path:
    """Draw the receipt and keep it on disk. Done when the payment is marked,
    before WhatsApp is told: WhatsApp fetches the file at once, possibly
    before the payment's database write is committed, and the file is there."""
    folder = _folder(lead.pay_token)
    if folder is None:
        raise ValueError("the lead has no usable pay token")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / filename(lead)
    target.write_bytes(build_pdf(lead, cfg, offer_title=offer_title))
    return target


def forget(token: str) -> None:
    """Remove a deleted lead's saved receipts."""
    folder = _folder(token)
    if folder is None or not folder.is_dir():
        return
    for item in folder.glob("*.pdf"):
        item.unlink(missing_ok=True)
    try:
        folder.rmdir()
    except OSError:
        pass


def stored(token: str, name: str) -> Path | None:
    """The saved receipt `name` for this pay token, when there is one."""
    folder = _folder(token)
    if folder is None or not NAME.fullmatch(name or ""):
        return None
    found = folder / name
    return found if found.is_file() else None


def build_pdf(lead: SftLead, cfg: SftConfig, *, offer_title: str | None = None) -> bytes:
    """The receipt as PDF bytes."""
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(18, 18, 18)
    pdf.add_page()
    money = _money_fn(lead.currency or cfg.currency)
    paid = Decimal(str(lead.amount_paid or 0))
    fee = max(Decimal(str(cfg.course_fee)), paid)
    saving = fee - paid if (lead.offer_id or lead.offer_code) and fee > paid else Decimal(0)
    if not saving:
        fee = paid

    # Band: logo, the course, and what this is.
    pdf.set_fill_color(*BRAND)
    pdf.rect(0, 0, 210, 38, style="F")
    if LOGO.exists():
        pdf.set_fill_color(255, 255, 255)
        pdf.rect(17, 9, 20, 20, style="F", round_corners=True, corner_radius=3)
        pdf.image(str(LOGO), x=18, y=10, w=18)
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(42, 11)
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(90, 8, COURSE)
    pdf.set_xy(42, 19)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(90, 5, "by Shero Home Food")
    pdf.set_xy(120, 11)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(72, 8, "ENROLLMENT RECEIPT", align="R")
    pdf.set_xy(120, 19)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(72, 5, _t(f"No. {number(lead)}"), align="R")

    # Status pill.
    pdf.set_xy(18, 46)
    if lead.test_payment:
        pdf.set_fill_color(254, 243, 199)
        pdf.set_text_color(*WARN)
        label = "TEST PAYMENT - NOBODY WAS CHARGED"
    else:
        pdf.set_fill_color(220, 252, 231)
        pdf.set_text_color(*SAVING)
        label = "PAID" if not lead.refunded_at else "REFUNDED"
    pdf.set_font("Helvetica", "B", 9)
    width = pdf.get_string_width(label) + 10
    pdf.cell(width, 7, label, fill=True, align="C")

    # Billed to | receipt details.
    top = 60
    pdf.set_xy(18, top)
    _label(pdf, "Billed to")
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*INK)
    pdf.cell(80, 6, _t(lead.name or lead.profile_name or "Student"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    for line in (f"WhatsApp +{lead.whatsapp_number}", lead.email or ""):
        if line:
            pdf.cell(80, 5, _t(line), new_x="LMARGIN", new_y="NEXT")

    rows = [("Receipt no.", number(lead)), ("Date paid", when(lead.paid_at, cfg)),
            ("Payment", "Test (no charge)" if lead.test_payment else "Card, through Stripe")]
    if lead.payment_intent_id:
        rows.append(("Reference", lead.payment_intent_id))
    pdf.set_xy(112, top)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*MUTED)
    pdf.cell(80, 5, "RECEIPT DETAILS", new_x="LEFT", new_y="NEXT")
    for key, value in rows:
        pdf.set_x(112)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*MUTED)
        pdf.cell(26, 5.5, key)
        pdf.set_font("Helvetica", "B" if key == "Receipt no." else "", 9)
        pdf.set_text_color(*INK)
        pdf.cell(54, 5.5, _fit(pdf, value, 54), align="R", new_x="LEFT", new_y="NEXT")
        pdf.set_x(112)

    # The course line.
    y = max(pdf.get_y(), top + 26) + 8
    pdf.set_xy(18, y)
    pdf.set_fill_color(*BRAND)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(134, 8, "  Description", fill=True)
    pdf.cell(40, 8, "Amount  ", fill=True, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_text_color(*INK)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(134, 6, "  " + COURSE)
    pdf.cell(40, 6, money(fee) + "  ", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*MUTED)
    for line in COURSE_LINES:
        pdf.cell(134, 4.5, "  " + line, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_draw_color(220, 226, 226)
    pdf.line(18, pdf.get_y(), 192, pdf.get_y())
    pdf.ln(3)

    # Totals.
    def total_row(label: str, amount: str, *, bold=False, colour=INK, size=10):
        pdf.set_x(100)
        pdf.set_font("Helvetica", "B" if bold else "", size)
        pdf.set_text_color(*colour)
        pdf.cell(52, 7, label)
        pdf.cell(40, 7, amount + "  ", align="R", new_x="LMARGIN", new_y="NEXT")

    total_row("Course fee", money(fee))
    if saving:
        code = lead.offer_code or ""
        what = " ".join(p for p in (f"Offer {code}".strip(), f"({offer_title})" if offer_title
                                    and offer_title != code else "") if p)
        total_row(_fit(pdf, what, 50), "-" + money(saving), colour=SAVING)
    pdf.set_draw_color(*BRAND)
    pdf.line(100, pdf.get_y() + 1, 192, pdf.get_y() + 1)
    pdf.ln(3)
    pdf.set_x(100)
    pdf.set_fill_color(*BRAND_LIGHT)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(*BRAND)
    pdf.cell(52, 10, "  Total paid", fill=True)
    pdf.cell(40, 10, money(paid) + "  ", fill=True, align="R", new_x="LMARGIN", new_y="NEXT")
    if saving:
        pdf.set_x(100)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*SAVING)
        pdf.cell(92, 6, f"You saved {money(saving)}.", align="R", new_x="LMARGIN", new_y="NEXT")

    # Next steps.
    pdf.ln(8)
    pdf.set_x(18)
    _label(pdf, "Your next steps")
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(*INK)
    steps = [
        "Open your course" + (f": {cfg.course_url}" if cfg.course_url
                              else " from the link we send you on WhatsApp") + ".",
        "Work through the 5 days in order: each day has its video lessons and quizzes.",
        "Prepare your assigned items with the standard recipe and upload photos for review.",
        "Your certificate is issued once Shero's review approves them.",
    ]
    for i, step in enumerate(steps, 1):
        pdf.set_x(18)
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_text_color(*BRAND)
        pdf.cell(7, 5.5, f"{i}.")
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(*INK)
        pdf.multi_cell(167, 5.5, _t(step), new_x="LMARGIN", new_y="NEXT")

    # Footer.
    pdf.ln(8)
    pdf.set_draw_color(220, 226, 226)
    pdf.line(18, pdf.get_y(), 192, pdf.get_y())
    pdf.ln(3)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*MUTED)
    lines = []
    if lead.test_payment:
        lines.append("This receipt is from a test payment: no card was charged.")
    else:
        lines.append("Paid online by card through Stripe. Keep this receipt for your records.")
    contact = settings.whatsapp_business_number
    if contact and not str(contact).startswith("CHANGE"):
        lines.append(f"Questions? Message us on WhatsApp: +{str(contact).lstrip('+')}")
    lines.append("Thank you for enrolling in Shero Food Training!")
    for line in lines:
        pdf.cell(0, 4.5, _t(line), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


# --- helpers -----------------------------------------------------------------
def _label(pdf: FPDF, text: str) -> None:
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*MUTED)
    pdf.cell(0, 5, text.upper(), new_x="LMARGIN", new_y="NEXT")


def _money_fn(currency: str):
    code = (currency or "usd").upper()

    def money(amount) -> str:
        value = Decimal(str(amount or 0))
        return f"${value:,.2f}" if code == "USD" else f"{code} {value:,.2f}"

    return money


def when(stamp, cfg: SftConfig) -> str:
    if stamp is None:
        return "-"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(cfg.timezone)
    except Exception:  # noqa: BLE001 - validated on save; UTC if not
        zone = timezone.utc
    local = stamp.astimezone(zone)
    return local.strftime("%d %b %Y, %I:%M %p ") + (local.tzname() or "")


def _fit(pdf: FPDF, text: str, width: float) -> str:
    text = _t(text)
    if pdf.get_string_width(text) <= width:
        return text
    while text and pdf.get_string_width(text + "...") > width:
        text = text[:-1]
    return text.rstrip() + "..."


def _t(text: str) -> str:
    """The built-in PDF fonts are Latin-1; replace anything outside it."""
    return str(text).encode("latin-1", "replace").decode("latin-1")
