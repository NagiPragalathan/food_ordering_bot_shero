"""The fields the bot adds to Shero's Zoho CRM, as Zoho's field-create API wants them.

Used by scripts/setup_zoho_crm.py, which compares these against the live
modules and creates only what is missing. Zoho derives each field's API name
from its label ("Bot Stage" -> "Bot_Stage"); fields.py holds those names.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.models.enums import LeadStage
from app.integrations.zoho import fields as f


@dataclass(frozen=True)
class FieldSpec:
    module: str
    api_name: str          # what Zoho will call it; checked after creation
    body: dict             # one entry of the create API's "fields" list


def _text(label: str, length: int = 255) -> dict:
    return {"field_label": label, "data_type": "text", "length": length}


def _money(label: str) -> dict:
    return {"field_label": label, "data_type": "currency", "length": 16, "decimal_place": 2}


FIELDS: tuple[FieldSpec, ...] = (
    # Leads: the spec's Lead data (section 2) that has no home in the layout.
    FieldSpec(f.LEADS, f.L_BOT_STAGE, {
        "field_label": "Bot Stage", "data_type": "picklist",
        "pick_list_values": [{"display_value": s.value, "actual_value": s.value}
                             for s in LeadStage],
    }),
    FieldSpec(f.LEADS, f.L_BOT_STAGE_HISTORY, {
        "field_label": "Bot Stage History", "data_type": "textarea",
        "length": 2000, "textarea": {"type": "small"},
    }),
    FieldSpec(f.LEADS, f.L_SELECTED_OUTLET, _text("Selected Outlet", 120)),
    FieldSpec(f.LEADS, f.L_DISTANCE_KM, {
        "field_label": "Distance KM", "data_type": "double",
        "length": 8, "decimal_place": 2,
    }),
    # Orders: the spec's Order data (section 2) that has no home yet.
    FieldSpec("Orders", f.O_LEAD, {
        "field_label": "Lead", "data_type": "lookup",
        # display_label names the related list on the Lead page.
        "lookup": {"module": {"api_name": f.LEADS}, "display_label": "Orders"},
    }),
    FieldSpec("Orders", f.O_OUTLET_NAME, _text("Outlet Name", 120)),
    FieldSpec("Orders", f.O_DELIVERY_SLOT, _text("Delivery Slot", 80)),
    FieldSpec("Orders", f.O_DELIVERY_CHARGE, _money("Delivery Charge")),
    FieldSpec("Orders", f.O_TAXES_AND_FEES, _money("Taxes and Fees")),
    FieldSpec("Orders", f.O_ORDER_TOTAL, _money("Order Total")),
    FieldSpec("Orders", f.O_STRIPE_PAYMENT_ID, _text("Stripe Payment ID", 255)),
    FieldSpec("Orders", f.O_DELIVERED_TIME, {"field_label": "Delivered Time",
                                             "data_type": "datetime"}),
)

# An option added to an existing picklist: (module, field api name, option).
PICKLIST_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("Orders", f.O_CHANNEL, f.CHANNEL_WHATSAPP_BOT),
)


def missing_fields(existing: dict[str, set[str]]) -> list[FieldSpec]:
    """Specs whose field is not in `existing` ({module: {api names}})."""
    return [spec for spec in FIELDS
            if spec.api_name not in existing.get(spec.module, set())]
