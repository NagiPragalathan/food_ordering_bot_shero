"""Create and inspect WhatsApp message templates on the Gallabox account.

Templates are the only way to message a customer who has not written in for
24 hours, so every one in `templates.py` has to exist and be approved on the
WABA before the bot can use it.

There are two routes to create one. Meta's Graph API needs a system-user token
for the WABA, which is owned by the client's Business Manager and not
something this service holds. Gallabox exposes the same thing at
`/accounts/{accountId}/whatsappTemplates` and forwards it to Meta on our
behalf, authenticated with the ordinary API key we already use for sending -
so that is the route used here.

`components()` builds Meta's own component array, which both routes take
verbatim; `scripts/submit_templates.py` reuses it for the Graph payload.
"""

from __future__ import annotations

from app.core.config import is_unset, settings
from app.core.exceptions import ConfigurationError
from app.core.logging import get_logger
from app.integrations.gallabox.client import gallabox
from app.integrations.gallabox.templates import TemplateSpec

log = get_logger(__name__)

# Meta shows these to the human reviewer next to each {{n}}. They are examples
# only - nothing here is ever sent to a customer.
EXAMPLES = {
    "customer_name": "Asha",
    "order_number": "SHO-260923-AB12X",
    "amount": "29.42",
    "slot_label": "Wed 23 Sep, 7:00 PM - 8:00 PM",
    "reason": "the kitchen is at capacity",
    "eta": "25 minutes",
    "outlet_name": "Shero Kitchen",
    "items": "2 x Drumstick Sambar, 1 x Beans Sambar",
    "delivery_address": "6360 Lawyers Hill Road, Apt 4, 21075",
}

EXAMPLE_ORDER_NUMBER = "SHO-260923-AB12X"
EXAMPLE_RECEIPT_TOKEN = "IlNITy0yNjA5MjMtQUIxMlgi.hN0bK3Yx8QeWm2cVdL5pRs7uTzA"
# What a signed ordering-link token looks like, for Meta's reviewer.
EXAMPLE_LINK_TOKEN = "IjI4NTVjM2JmLTliY2YtNGU2NS04MDlmLWM5ZjUxMDk0YjNjYyI.arW2Wg.xIM6yYeY74aaYz9HVSnW7F6-Kmg"


def example_for(param: str) -> str:
    """A believable sample value for one body parameter."""
    return EXAMPLES.get(param, param.replace("_", " ").title())


def _order_page_base_url() -> str:
    """Base of the View Menu button: the web ordering page, token appended."""
    base = settings.public_base_url
    if is_unset(base) or "localhost" in base:
        raise ConfigurationError(
            "PUBLIC_BASE_URL must be the public address before creating a "
            "template with a View Menu button - the URL is baked into the "
            "approved template."
        )
    return f"{base.rstrip('/')}/order"


def _receipt_base_url() -> str:
    """Base of the Download Invoice button: the PDF invoice, receipt token appended."""
    return _order_page_base_url().removesuffix("/order") + "/receipt"


def _button_base_url() -> str:
    """Base of the dynamic-URL button, without a trailing slash.

    The Pay Now button points at our short redirect rather than the Stripe
    checkout URL, which is far past Meta's button length limit.
    """
    base = settings.pay_redirect_base_url
    if is_unset(base):
        raise ConfigurationError(
            "PAY_REDIRECT_BASE_URL must be set before creating a template with "
            "a Pay Now button - the URL is baked into the approved template."
        )
    return base.rstrip("/")


def check_body(spec: TemplateSpec) -> None:
    """Fail on the body rules Meta enforces, before a template is submitted.

    A rejected template is not free: it sits in the account as an error until
    somebody deletes it by hand, and the name stays taken meanwhile. Cheaper
    to refuse it here.
    """
    text = spec.sample_body.strip()
    # Meta reads a variable with only punctuation after it as the end of the
    # body, so "delivery at {{4}}." counts as ending on a variable.
    stripped = text.rstrip(" .!?,:;-—")
    if stripped.endswith("}}") or text.lstrip().startswith("{{"):
        raise ValueError(
            f"template '{spec.name}': a body may not start or end with a "
            f"variable. Add words around it - trailing punctuation does not "
            f"count. Body ends: ...{text[-40:]!r}"
        )


def components(spec: TemplateSpec) -> list[dict]:
    """Meta's `components` array for one template.

    Accepted as-is by both Gallabox and the Graph API.
    """
    check_body(spec)
    body: dict = {"type": "BODY", "text": spec.sample_body}
    if spec.param_count:
        body["example"] = {"body_text": [[example_for(p) for p in spec.params]]}

    blocks: list[dict] = [body]
    buttons: list[dict] = []

    if spec.button_kind == "dynamic_url":
        if spec.url_base == "order":
            base, example = _order_page_base_url(), EXAMPLE_LINK_TOKEN
        elif spec.url_base == "receipt":
            base, example = _receipt_base_url(), EXAMPLE_RECEIPT_TOKEN
        else:
            base, example = _button_base_url(), EXAMPLE_ORDER_NUMBER
        buttons.append({
            "type": "URL",
            "text": spec.button_label or "Open",
            # Meta appends the runtime parameter to this base, so the
            # approved template owns the domain: changing it later means
            # submitting the template again.
            "url": f"{base}/{{{{1}}}}",
            "example": [f"{base}/{example}"],
        })
    elif spec.button_kind == "static_url":
        buttons.append({"type": "URL", "text": spec.button_label or "Open",
                        "url": spec.button_url})
    # Quick replies may sit alongside a URL button (order_summary: Pay Now,
    # then Change menu and Update location). Meta wants each kind grouped,
    # which appending them after the URL satisfies.
    if spec.quick_replies:
        buttons.extend({"type": "QUICK_REPLY", "text": label}
                       for label in spec.quick_replies)

    if buttons:
        blocks.append({"type": "BUTTONS", "buttons": buttons})
    return blocks


def creation_payload(spec: TemplateSpec, *, channel_id: str | None = None) -> dict:
    """The body Gallabox expects when creating one template."""
    return {
        "channelId": channel_id or settings.gallabox_channel_id,
        "name": spec.name,
        # Matches the language the send path uses in `messages.template_message`.
        # A template approved as "en" cannot be sent as "en_US".
        "language": "en",
        "category": spec.category,
        # Let Meta re-file a template it judges to be a different category
        # rather than rejecting it outright.
        "allow_category_change": True,
        "components": components(spec),
    }


def _account_path() -> str:
    if is_unset(settings.gallabox_account_id):
        raise ConfigurationError(
            "GALLABOX_ACCOUNT_ID is not set. It is the 24-hex id in any "
            "Gallabox dashboard URL (/accounts/<this>/...). Sending messages "
            "does not need it; creating templates does."
        )
    return f"/accounts/{settings.gallabox_account_id}/whatsappTemplates"


async def list_templates(*, channel_id: str | None = None) -> list[dict]:
    """Every template on the account, or only those on one channel.

    Pass `channel_id=""` for all channels; the default is the channel the bot
    sends from, which is the only one whose templates it can actually use.
    """
    wanted = settings.gallabox_channel_id if channel_id is None else channel_id
    rows = await _all_pages()
    if not wanted:
        return rows
    return [row for row in rows if row.get("channelId") == wanted]


PAGE_SIZE = 100      # Gallabox's largest page; without `limit` it sends 20
MAX_PAGES = 50       # a guard against a server that ignores `page`


async def _all_pages() -> list[dict]:
    """Every template on the account. The list is paged: reading only the
    first page made approved templates look missing, and the bot then
    skipped sending them."""
    rows: list[dict] = []
    seen: set = set()
    for page in range(1, MAX_PAGES + 1):
        found = await gallabox.get(_account_path(), params={"page": page, "limit": PAGE_SIZE})
        batch = found if isinstance(found, list) else []
        fresh = [r for r in batch if (r.get("id") or r.get("name"), r.get("channelId")) not in seen]
        seen.update((r.get("id") or r.get("name"), r.get("channelId")) for r in fresh)
        rows.extend(fresh)
        if len(batch) < PAGE_SIZE or not fresh:
            return rows
    log.warning("template_list_page_limit_reached", pages=MAX_PAGES)
    return rows


# Note: the dev API creates and lists templates but does not edit or delete
# them - PUT and DELETE on a template both 404. A template that comes back
# `error` keeps its name, so correcting one means deleting it in the Gallabox
# dashboard first; `check_body` exists to make that rare.


async def create_template(spec: TemplateSpec, *,
                          channel_id: str | None = None) -> dict:
    """Create one template and submit it to Meta for approval.

    Returns the created record. Approval is asynchronous: the record comes
    back `pending` and Meta moves it to `approved` or `rejected` later, which
    `list_templates` will show.
    """
    created = await gallabox.post(_account_path(),
                                  json=creation_payload(spec, channel_id=channel_id))
    record = created if isinstance(created, dict) else {}
    log.info("whatsapp_template_created", name=spec.name,
             status=record.get("status"), template_id=record.get("whatsappTemplateId"))
    return record
