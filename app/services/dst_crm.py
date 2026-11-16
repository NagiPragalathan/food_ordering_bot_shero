"""DST India's own CRM (its "Lead Pipeline & Tracker"): where an ordering
customer sits in it.

DST India does not use Zoho. Its CRM has 13 stages, named exactly as the CRM
shows them (Shero CRM Stages & Template Mapping, Nov 2026):

    Welcome, Requirement Capture, RNR 1, RNR 2, RNR Final, Quotation Sent,
    Quotation Follow-up, Quotation Expiring, Revised Quotation,
    Order Confirmed, Delivery Status, Lost Feedback, Win-back

The ordering bot ("Hungry Now? Order Food", the Order Now vertical) uses four
of them. Our funnel maps onto them as:

    a new chat (New Enquiry)                    -> Welcome
    every ordering step, payment link included  -> Requirement Capture
    paid (Converted / Paid & Slot Booked)       -> Order Confirmed
    out for delivery, delivered                 -> Delivery Status

The quotation stages belong to the catering forms (Party Order, Food
Subscription, Sweets & Snacks), which DST's Gallabox bot flow runs, and the
RNR / Lost Feedback / Win-back stages to its follow-ups.

How a stage reaches the CRM is not known yet: the client's documents say the
CRM page is fed from Gallabox, and access to it has not been given. So
`push_stage` is the one place that will send it, and today it only records
the stage in the log. Nothing here invents an API.
"""

from __future__ import annotations

from app.core import tenants
from app.core.logging import get_logger
from app.db.models import Customer, LeadStage, OrderStage

log = get_logger(__name__)

WELCOME = "Welcome"
REQUIREMENT = "Requirement Capture"
RNR1 = "RNR 1"
RNR2 = "RNR 2"
RNR_FINAL = "RNR Final"
QUOTE_SENT = "Quotation Sent"
QUOTE_FOLLOWUP = "Quotation Follow-up"
QUOTE_EXPIRING = "Quotation Expiring"
REVISED = "Revised Quotation"
CONFIRMED = "Order Confirmed"
DELIVERY = "Delivery Status"
LOST = "Lost Feedback"
WINBACK = "Win-back"

STAGES: tuple[str, ...] = (WELCOME, REQUIREMENT, RNR1, RNR2, RNR_FINAL, QUOTE_SENT,
                           QUOTE_FOLLOWUP, QUOTE_EXPIRING, REVISED, CONFIRMED, DELIVERY,
                           LOST, WINBACK)

# The vertical an ordering customer belongs to in the CRM, and the lead
# status the client's flow document gives it.
VERTICAL = "Order Now"
LEAD_STATUS = "Order Now - New Lead"

FOR_LEAD_STAGE: dict[str, str] = {
    LeadStage.NEW_ENQUIRY: WELCOME,
    LeadStage.DETAILS_CAPTURED: REQUIREMENT,
    LeadStage.CUISINE_SELECTED: REQUIREMENT,
    LeadStage.CART_CREATED: REQUIREMENT,
    LeadStage.OUTLET_SELECTED: REQUIREMENT,
    LeadStage.SLOT_SELECTED: REQUIREMENT,
    LeadStage.PAYMENT_LINK_SENT: REQUIREMENT,
    LeadStage.PAYMENT_ABANDONED: REQUIREMENT,
    LeadStage.PAYMENT_FAILED: REQUIREMENT,
    LeadStage.CONVERTED: CONFIRMED,
    # Not Serviceable has no stage of its own in the CRM.
}

FOR_ORDER_STAGE: dict[str, str] = {
    OrderStage.PAID_SLOT_BOOKED: CONFIRMED,
    OrderStage.SENT_TO_KITCHEN: CONFIRMED,
    OrderStage.OUT_FOR_DELIVERY: DELIVERY,
    OrderStage.DELIVERED: DELIVERY,
    # Pending Payment, Cancelled and Refunded have no stage of their own.
}

NOT_CONNECTED = "DST CRM access not provided yet"


def in_use() -> bool:
    """The DST CRM is DST India's: only while serving it."""
    return tenants.current() == tenants.DST


def stage_for(lead_stage: str | LeadStage | None) -> str | None:
    """The DST CRM stage for one of our funnel stages, or None."""
    return FOR_LEAD_STAGE.get(str(lead_stage)) if lead_stage else None


def stage_for_order(order_stage: str | OrderStage | None) -> str | None:
    """The DST CRM stage for one of our order stages, or None."""
    return FOR_ORDER_STAGE.get(str(order_stage)) if order_stage else None


async def push_stage(customer: Customer, stage: str, *, ours: str = "") -> bool:
    """Put `customer` in `stage` in the DST CRM. True once it went through.

    The seam for the CRM connection. Until access is given it records the
    stage next to ours in the log and returns False; it never raises."""
    log.info("dst_crm_stage", customer_id=str(customer.id), stage=stage, our_stage=ours,
             vertical=VERTICAL, pushed=False, reason=NOT_CONNECTED)
    return False


async def record_lead_stage(customer: Customer, lead_stage: str | LeadStage) -> str | None:
    """Our funnel moved: the DST CRM stage it means, sent on. None when this
    is not DST India or the stage has no DST CRM counterpart."""
    if not in_use():
        return None
    stage = stage_for(lead_stage)
    if stage:
        await push_stage(customer, stage, ours=str(lead_stage))
    return stage


async def record_order_stage(customer: Customer | None,
                             order_stage: str | OrderStage) -> str | None:
    """An order moved (paid, out for delivery...): its DST CRM stage, sent on."""
    if not in_use() or customer is None:
        return None
    stage = stage_for_order(order_stage)
    if stage:
        await push_stage(customer, stage, ours=str(order_stage))
    return stage
