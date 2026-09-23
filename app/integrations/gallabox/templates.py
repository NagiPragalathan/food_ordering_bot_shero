"""The 10 WhatsApp templates from section 3 of the spec.

Each entry is the contract we hold Meta to: template name, category, and the
ordered body parameters. `render()` validates the argument count before the
call leaves the process, so a mismatch surfaces as a clear error here instead
of a generic 400 from Gallabox.

Templates with a dynamic-URL button take a single button parameter: the URL
suffix appended to the base configured in Meta. Per the spec note, the Pay Now
button points at the short redirect (pay.<domain>/{{1}}), not the raw Stripe
URL, because Stripe checkout URLs exceed the button limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Category = Literal["UTILITY", "MARKETING"]
ButtonKind = Literal["none", "dynamic_url", "quick_reply"]


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    category: Category
    trigger: str
    params: tuple[str, ...]
    sample_body: str
    button_kind: ButtonKind = "none"
    button_label: str | None = None
    quick_replies: tuple[str, ...] = field(default_factory=tuple)

    @property
    def param_count(self) -> int:
        return len(self.params)


PAYMENT_LINK = TemplateSpec(
    name="payment_link",
    category="UTILITY",
    trigger="Order summary confirmed (step 14-15)",
    params=("customer_name", "order_number", "amount", "slot_label"),
    sample_body=(
        "Hi {{1}}, your Shero order #{{2}} comes to ${{3}} for the {{4}} delivery "
        "slot. Tap below to pay securely. The link is valid for 30 minutes."
    ),
    button_kind="dynamic_url",
    button_label="Pay Now",
)

PAYMENT_REMINDER = TemplateSpec(
    name="payment_reminder",
    category="UTILITY",
    trigger="15 minutes unpaid (step 15)",
    params=("customer_name", "order_number", "slot_label"),
    sample_body=(
        "Hi {{1}}, your order #{{2}} is waiting for payment. Complete it to keep "
        "your {{3}} delivery slot."
    ),
    button_kind="dynamic_url",
    button_label="Pay Now",
)

PAYMENT_SUCCESS = TemplateSpec(
    name="payment_success",
    category="UTILITY",
    trigger="Stripe payment confirmed (step 16)",
    params=("order_number", "amount", "outlet_name", "slot_label"),
    sample_body=(
        "Payment received! Your order #{{1}} of ${{2}} is confirmed from {{3}} "
        "for delivery at {{4}}."
    ),
)

PAYMENT_FAILED = TemplateSpec(
    name="payment_failed",
    category="UTILITY",
    trigger="Stripe payment failed (step 16)",
    params=("customer_name", "order_number"),
    sample_body=(
        "Hi {{1}}, your payment for order #{{2}} didn't go through. Please try again."
    ),
    button_kind="dynamic_url",
    button_label="Retry Payment",
)

PAYMENT_EXPIRED = TemplateSpec(
    name="payment_expired",
    category="MARKETING",
    trigger="Link expired unpaid (step 15)",
    params=("customer_name",),
    sample_body="Hi {{1}}, your cart is still saved. Want to complete your Shero order?",
    button_kind="dynamic_url",
    button_label="Order Now",
)

REFUND_PROCESSED = TemplateSpec(
    name="refund_processed",
    category="UTILITY",
    trigger="Stripe refund issued",
    params=("amount", "order_number"),
    sample_body=(
        "Your refund of ${{1}} for order #{{2}} has been processed. It may take "
        "5-10 business days to reflect."
    ),
)

ORDER_OUT_FOR_DELIVERY = TemplateSpec(
    name="order_out_for_delivery",
    category="UTILITY",
    trigger="Outlet marks Out for Delivery (step 18)",
    params=("order_number",),
    sample_body="Your order #{{1}} is on its way!",
)

ORDER_DELIVERED = TemplateSpec(
    name="order_delivered",
    category="UTILITY",
    trigger="Outlet marks Delivered (step 18)",
    params=("order_number",),
    sample_body="Your order #{{1}} has been delivered. Enjoy your meal!",
)

ORDER_CANCELLED = TemplateSpec(
    name="order_cancelled",
    category="UTILITY",
    trigger="Order cancelled by outlet",
    params=("order_number",),
    sample_body=(
        "Sorry, your order #{{1}} has been cancelled. A full refund has been started."
    ),
    button_kind="quick_reply",
    button_label="Talk to Us",
    quick_replies=("Talk to Us",),
)

FEEDBACK_REQUEST = TemplateSpec(
    name="feedback_request",
    category="MARKETING",
    trigger="30 minutes after delivery (step 19)",
    params=("order_number",),
    sample_body="How was your Shero meal today? Rate your order #{{1}}.",
    button_kind="quick_reply",
    quick_replies=("Great", "Good", "Poor"),
)

ALL_TEMPLATES: tuple[TemplateSpec, ...] = (
    PAYMENT_LINK,
    PAYMENT_REMINDER,
    PAYMENT_SUCCESS,
    PAYMENT_FAILED,
    PAYMENT_EXPIRED,
    REFUND_PROCESSED,
    ORDER_OUT_FOR_DELIVERY,
    ORDER_DELIVERED,
    ORDER_CANCELLED,
    FEEDBACK_REQUEST,
)

BY_NAME: dict[str, TemplateSpec] = {t.name: t for t in ALL_TEMPLATES}


def render(spec: TemplateSpec, *values: object, button_value: str | None = None) -> dict:
    """Build the Gallabox template block, validating arity first."""
    if len(values) != spec.param_count:
        raise ValueError(
            f"template '{spec.name}' expects {spec.param_count} parameters "
            f"{spec.params}, got {len(values)}"
        )
    if spec.button_kind == "dynamic_url" and not button_value:
        raise ValueError(f"template '{spec.name}' has a dynamic URL button and needs a button_value")

    from app.integrations.gallabox.messages import template_message

    button_values = {"0": [button_value]} if button_value else None
    return template_message(spec.name, [str(v) for v in values], button_values=button_values)
