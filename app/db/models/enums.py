"""Domain enumerations shared by the models, the CRM sync and the bot engine."""

from __future__ import annotations

from enum import StrEnum


class LeadStage(StrEnum):
    """Zoho Lead stages - section 2 of the spec, used for drop-off reporting."""

    NEW_ENQUIRY = "New Enquiry"            # step 1
    DETAILS_CAPTURED = "Details Captured"  # steps 3-4
    CUISINE_SELECTED = "Cuisine Selected"  # step 6
    CART_CREATED = "Cart Created"          # step 8
    NOT_SERVICEABLE = "Not Serviceable"    # step 6a / 10
    OUTLET_SELECTED = "Outlet Selected"    # step 11
    SLOT_SELECTED = "Slot Selected"        # steps 12-13
    PAYMENT_LINK_SENT = "Payment Link Sent"    # steps 14-15
    PAYMENT_ABANDONED = "Payment Abandoned"    # step 15
    PAYMENT_FAILED = "Payment Failed"          # step 16
    CONVERTED = "Converted"                    # step 16


class OrderStage(StrEnum):
    """Zoho Order stages - set after the Lead converts to a Contact."""

    PENDING_PAYMENT = "Pending Payment"
    PAID_SLOT_BOOKED = "Paid & Slot Booked"
    SENT_TO_KITCHEN = "Sent to Kitchen"
    OUT_FOR_DELIVERY = "Out for Delivery"
    DELIVERED = "Delivered"
    CANCELLED = "Cancelled"
    REFUNDED = "Refunded"


class PaymentStatus(StrEnum):
    NOT_STARTED = "not_started"
    LINK_SENT = "link_sent"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"
    REFUNDED = "refunded"


class SlotHoldStatus(StrEnum):
    HELD = "held"        # payment pending, slot reserved (spec step 13)
    BOOKED = "booked"    # payment confirmed (spec step 16)
    RELEASED = "released"  # link expired / payment failed


class ConversationStep(StrEnum):
    """Where a customer currently sits in the flow (spec section 1).

    Values are persisted, so renaming one needs a data migration.
    """

    START = "start"                        # step 1-2: entry / returning check
    AWAIT_NAME = "await_name"              # step 3
    AWAIT_EMAIL = "await_email"            # step 4
    MAIN_MENU = "main_menu"                # step 5: Order Online / Talk to Us
    CUISINE_MENU = "cuisine_menu"          # step 6: cuisines + Check Availability
    AWAIT_AVAILABILITY_LOCATION = "await_availability_location"  # step 6a
    # Step 7 browsing. The menu has ~290 items and a WhatsApp list holds 10
    # rows, so it is navigated cuisine -> category -> item.
    BROWSING_CATEGORIES = "browsing_categories"
    BROWSING_ITEMS = "browsing_items"
    AWAIT_QUANTITY = "await_quantity"
    CART_REVIEW = "cart_review"            # step 8: review and check out
    AWAIT_LOCATION = "await_location"      # step 9: mandatory before payment
    AWAIT_ADDRESS = "await_address"                    # step 12
    AWAIT_APARTMENT = "await_apartment"                # step 12
    AWAIT_INSTRUCTIONS = "await_instructions"          # step 12
    AWAIT_CONTACT_NUMBER = "await_contact_number"      # step 12
    AWAIT_SLOT_CHOICE = "await_slot_choice"            # step 13
    AWAIT_SUMMARY_CONFIRM = "await_summary_confirm"    # step 14
    AWAIT_PAYMENT = "await_payment"                    # step 15-16
    AWAIT_FEEDBACK = "await_feedback"                  # step 19
    HANDED_OVER = "handed_over"            # "Talk to Us" -> Gallabox agent
    COMPLETED = "completed"


# Steps 12's four questions asked in order; kept as data so the sequence can be
# reordered or trimmed without touching the handler logic.
DELIVERY_DETAIL_SEQUENCE: tuple[ConversationStep, ...] = (
    ConversationStep.AWAIT_ADDRESS,
    ConversationStep.AWAIT_APARTMENT,
    ConversationStep.AWAIT_INSTRUCTIONS,
    ConversationStep.AWAIT_CONTACT_NUMBER,
)
