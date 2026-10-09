"""A template's message as an ordinary WhatsApp message, for when the
template is not approved (or Gallabox refuses it).

Gallabox accepts a send for a template that is not approved and then drops
it without an error, so the customer would get nothing. Every template send
goes through GallaboxClient.send_template, which checks approval first and
sends this instead:

  * the same body, with the parameters filled in
  * a URL button (Pay Now, Download Invoice, View Menu) becomes a link button
    with the same label and the same address
  * quick replies (Great / Good / Poor, Order Now) become reply buttons whose
    id is the label, which is exactly what a template quick-reply tap sends
    back, so the conversation handles both the same way
  * a template with a link and quick replies keeps the link; the quick
    replies are left out (an interactive message holds one or the other)
  * except a fixed link (the welcome's www.shero.us): it goes into the text,
    where WhatsApp makes it tappable, so the reply buttons can stay - the
    customer keeps both choices

A non-template message is only delivered inside WhatsApp's 24-hour window.
Nearly every template here is sent right after the customer wrote or paid,
so it is; one sent later (a delivery update hours after ordering) may not be.
"""

from __future__ import annotations

import re

from app.core.config import settings
from app.integrations.gallabox import messages as m
from app.integrations.gallabox.templates import TemplateSpec

MAX_BUTTON_LABEL = 20


def body_text(spec: TemplateSpec, values: tuple) -> str:
    """The template body with {{1}}, {{2}}... replaced by the values."""
    def value(match: re.Match) -> str:
        index = int(match.group(1)) - 1
        return str(values[index]) if 0 <= index < len(values) else ""
    return re.sub(r"\{\{(\d+)\}\}", value, spec.sample_body)


def button_url(spec: TemplateSpec, button_value: str) -> str:
    """Where the template's URL button would have gone."""
    public = settings.public_base_url.rstrip("/")
    base = {"pay": settings.pay_redirect_base_url.rstrip("/"),
            "order": f"{public}/order",
            "receipt": f"{public}/receipt"}[spec.url_base]
    return f"{base}/{button_value}"


def message(spec: TemplateSpec, values: tuple, button_value: str | None) -> dict:
    """The message payload to send in place of the template."""
    body = body_text(spec, values)
    if spec.button_kind == "dynamic_url" and button_value:
        return m.cta_url_message(body, url=button_url(spec, button_value),
                                 display_text=(spec.button_label or "Open")[:MAX_BUTTON_LABEL])
    if spec.button_kind == "static_url" and spec.button_url:
        body = f"{body}\n\n{spec.button_label or 'Open'}: {spec.button_url}"
    if spec.quick_replies:
        buttons = [m.Button(id=label, title=label)
                   for label in spec.quick_replies[:m.MAX_REPLY_BUTTONS]]
        return m.button_message(body, buttons)
    return m.text_message(body)
