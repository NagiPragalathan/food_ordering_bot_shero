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
# Where a dynamic URL button points: the short payment redirect, or the web
# ordering page (whose suffix is the customer's signed link token).
UrlBase = Literal["pay", "order"]


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
    url_base: UrlBase = "pay"

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
    # Ends on words, not on {{4}}: Meta rejects a body whose last thing is a
    # variable, counting a trailing full stop as no ending at all.
    sample_body=(
        "Payment received! Your order #{{1}} of ${{2}} is confirmed from {{3}} "
        "for delivery at {{4}}. Thank you for ordering with Shero!"
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
    sample_body=(
        "How was your Shero meal today? Rate order #{{1}} using the buttons below."
    ),
    button_kind="quick_reply",
    quick_replies=("Great", "Good", "Poor"),
)

WELCOME = TemplateSpec(
    name="shero_welcome",
    category="MARKETING",
    trigger="Business-initiated opener, outside the 24-hour window",
    params=("customer_name",),
    sample_body=(
        "\U0001F44B Hi {{1}}! Welcome to *Shero Home Food* ❤️\U0001F1FA\U0001F1F8\n\n"
        "Looking for a delicious Indian vegetarian meal delivered to your home? "
        "\U0001F371\n\n"
        "We make ordering simple. Tap the button below to browse our menu, "
        "build your cart and pay securely."
    ),
    # A tapped button beats "reply ORDER": the payload reaches the engine as a
    # button reply, which `_handle_global_intent` starts the flow on.
    button_kind="quick_reply",
    quick_replies=("Order Now",),
)

ORDER_SUMMARY = TemplateSpec(
    name="order_summary",
    category="UTILITY",
    trigger="Web order confirmed - summary, then Pay Now / Change menu / Update location",
    params=("customer_name", "order_number", "items", "delivery_address",
            "slot_label", "amount"),
    # Parameters cannot carry line breaks (Meta rejects them at send time), so
    # the structure lives in the approved body and the items arrive as one
    # comma-separated line.
    sample_body=(
        "Hi {{1}}, here is your Shero order #{{2}}.\n\n"
        "*Items:* {{3}}\n"
        "*Deliver to:* {{4}}\n"
        "*Delivery:* {{5}}\n"
        "*Total:* ${{6}}\n\n"
        "Tap Pay Now to pay securely - the link is valid for 30 minutes. "
        "Need a change? Use the buttons below."
    ),
    # Pay Now is button 0, so its URL suffix is sent as button parameter "0".
    # The two quick replies arrive back as button replies carrying their text.
    button_kind="dynamic_url",
    button_label="Pay Now",
    quick_replies=("Change menu", "Update location"),
)

MENU_LINK = TemplateSpec(
    name="menu_link",
    category="UTILITY",
    trigger="Order Now / new link - the web menu, with Get new link on the same message",
    params=(),
    # A plain link message (cta_url) may carry only its one button. A template
    # may mix a URL button with quick replies, which is the only way to have
    # View Menu and Get new link on one message.
    sample_body=(
        "Here is our full menu \U0001F35B\n\n"
        "Tap View Menu to browse all our dishes, add what you like to your cart "
        "and choose a delivery time - we will send your payment link right back "
        "here. The link is personal to you and works for a few hours."
    ),
    button_kind="dynamic_url",
    button_label="View Menu",
    quick_replies=("Get new link",),
    url_base="order",
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

# Everything this project creates on the WABA: the spec's ten, plus the
# welcome opener (reaching customers before they write in), the web order
# summary (Pay Now with Change menu / Update location) and the menu link
# (View Menu with Get new link), none of which the spec lists.
MANAGED_TEMPLATES: tuple[TemplateSpec, ...] = ALL_TEMPLATES + (
    WELCOME, ORDER_SUMMARY, MENU_LINK)

BY_NAME: dict[str, TemplateSpec] = {t.name: t for t in MANAGED_TEMPLATES}


def render(spec: TemplateSpec, *values: object, button_value: str | None = None) -> dict:
    """Build the Gallabox template block, validating arity first."""
    if len(values) != spec.param_count:
        raise ValueError(
            f"template '{spec.name}' expects {spec.param_count} parameters "
            f"{spec.params}, got {len(values)}"
        )
    if spec.button_kind == "dynamic_url" and not button_value:
        raise ValueError(f"template '{spec.name}' has a dynamic URL button and needs a button_value")

    from app.integrations.gallabox.messages import template_message, url_button_value

    # Every template here has its one URL button first (index 0); quick
    # replies after it need no runtime value.
    button_values = [url_button_value(0, button_value)] if button_value else None
    return template_message(spec.name, [str(v) for v in values], button_values=button_values)
