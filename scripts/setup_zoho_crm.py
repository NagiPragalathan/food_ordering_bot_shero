"""Prepare a Zoho CRM for the bot (docs/zoho-setup.md).

    python -m scripts.setup_zoho_crm           # dry run: list what is missing
    python -m scripts.setup_zoho_crm --apply   # create it

On a fresh CRM this creates the Orders and Order Items modules, every field
in schema.py (on those two, on Leads and Contacts, and on Zoho's own Products
and Vendors modules) and the Lead Source and Lead Status options. Only
missing things are created, so it is safe to run again; nothing is renamed or
deleted. After creating, it reads the modules back and checks each field got
the API name the bot uses (fields.py).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.config import settings
from app.core.exceptions import IntegrationError
from app.db.session import SessionFactory
from app.integrations.zoho import fields as f
from app.integrations.zoho import schema
from app.integrations.zoho.client import zoho_client
from app.services.settings_store import apply_overrides

BATCH = 5   # Zoho creates at most five fields per call

MANUAL_MODULE = """\
Zoho would not create the module ({error}).
Create it by hand, then run this again:
  Setup > Customization > Modules and Fields > Create New Module
  Plural: {plural}   Singular: {singular}   (API name {module})"""


def _module(name: str) -> str:
    """schema.py names the bot's modules; the live names are configurable."""
    return {schema.ORDERS: settings.zoho_orders_module,
            schema.ORDER_ITEMS: settings.zoho_order_items_module}.get(name, name)


def _expected_display_field(live_name: str) -> str:
    """The display field the bot writes: the order number, or "order / dish"."""
    if live_name == settings.zoho_orders_module:
        return settings.zoho_orders_name_field
    return f.I_NAME_DEFAULT


def _localised(body: dict) -> dict:
    """A field body with the bot's module names replaced by the live ones,
    so a lookup to "Orders" points at whatever the Orders module is called."""
    lookup = body.get("lookup")
    if not lookup:
        return body
    module = dict(lookup.get("module") or {})
    module["api_name"] = _module(module.get("api_name", ""))
    return {**body, "lookup": {**lookup, "module": module}}


async def _fields_by_module(modules: set[str]) -> dict[str, dict[str, dict]]:
    return {m: {row["api_name"]: row for row in await zoho_client.list_fields(m)}
            for m in sorted(modules)}


def _missing_options(live: dict[str, dict[str, dict]]) -> list[tuple[str, dict, str]]:
    out = []
    for module, api_name, option in schema.PICKLIST_OPTIONS:
        field = live.get(_module(module), {}).get(api_name)
        if field is None:
            print(f"  !! {_module(module)}.{api_name} does not exist - cannot add '{option}'")
            continue
        values = {v.get("display_value") for v in field.get("pick_list_values") or []}
        if option not in values:
            out.append((_module(module), field, option))
    return out


def _display_field(module: dict) -> str | None:
    """The module's display field, whichever shape Zoho returns it in."""
    value = module.get("display_field")
    if isinstance(value, dict):
        return value.get("api_name")
    return value or None


def _check_display_fields(modules: dict[str, dict | None]) -> bool:
    """True when every bot module's display field is the one the bot writes."""
    ok = True
    for live_name, meta in modules.items():
        display = _display_field(meta or {})
        expected = _expected_display_field(live_name)
        if display and display != expected:
            hint = (f"set ZOHO_ORDERS_NAME_FIELD={display}"
                    if live_name == settings.zoho_orders_module
                    else "the bot writes Name; rename the field's API name to Name")
            print(f"  !! {live_name}'s display field is {display}, not {expected}: {hint}")
            ok = False
    return ok


async def run(apply: bool) -> int:
    async with SessionFactory() as session:
        await apply_overrides(session)      # the refresh token lives in the DB

    print(f"Zoho data centre: {settings.zoho_data_center}")

    # The bot's own modules, by live name: {live name: (labels, metadata or None)}.
    own = {_module(name): (labels, await zoho_client.get_module(_module(name)))
           for name, labels in schema.MODULES.items()}
    _check_display_fields({name: meta for name, (_, meta) in own.items()})

    wanted = {_module(s.module) for s in schema.FIELDS}
    wanted |= {_module(m) for m, _, _ in schema.PICKLIST_OPTIONS}
    present = {m for m in wanted if m not in own or own[m][1] is not None}
    live = await _fields_by_module(present)
    existing = {m: set(rows) for m, rows in live.items()}
    missing = [s for s in schema.FIELDS
               if s.api_name not in existing.get(_module(s.module), set())]
    options = _missing_options(live)
    new_modules = [name for name, (_, meta) in own.items() if meta is None]

    for name in new_modules:
        print(f"  + module {name}")
    for spec in missing:
        print(f"  + field  {_module(spec.module)}.{spec.api_name} "
              f"({spec.body['data_type']}, label '{spec.body['field_label']}')")
    for mod, field, option in options:
        print(f"  + option '{option}' on {mod}.{field['api_name']}")
    if not new_modules and not missing and not options:
        print("Nothing to do - the modules, every field and every option are there.")
        return 0

    if not apply:
        print("\nDry run. Re-run with --apply to create these.")
        return 0

    for name in new_modules:
        labels = own[name][0]
        try:
            await zoho_client.create_module(name, labels["singular_label"],
                                            labels["plural_label"])
            print(f"created module {name}")
        except IntegrationError as exc:
            print(MANUAL_MODULE.format(error=exc.message, module=name,
                                       plural=labels["plural_label"],
                                       singular=labels["singular_label"]))
            return 2

    # Orders before Order Items: the latter's lookup needs the former to exist.
    order = {_module(name): i for i, name in enumerate(schema.MODULES)}
    for mod in sorted({_module(s.module) for s in missing},
                      key=lambda m: (order.get(m, -1), m)):
        specs = [s for s in missing if _module(s.module) == mod]
        for start in range(0, len(specs), BATCH):
            batch = specs[start:start + BATCH]
            await zoho_client.create_fields(mod, [_localised(s.body) for s in batch])
            print(f"created {len(batch)} field(s) on {mod}")
    grouped: dict[str, tuple[str, dict, list[str]]] = {}
    for mod, field, option in options:
        grouped.setdefault(field["id"], (mod, field, []))[2].append(option)
    for mod, field, opts in grouped.values():
        await zoho_client.add_picklist_options(mod, field["id"], opts)
        print(f"added {', '.join(opts)} to {mod}.{field['api_name']}")

    # Zoho names each field from its label; make sure it matches fields.py.
    after = {m: set(rows) for m, rows in (await _fields_by_module(wanted)).items()}
    wrong = [s for s in schema.FIELDS if s.api_name not in after.get(_module(s.module), set())]
    for spec in wrong:
        print(f"  !! {_module(spec.module)}: expected API name {spec.api_name} "
              f"for '{spec.body['field_label']}' - update fields.py to match Zoho")
    created = {name: await zoho_client.get_module(name) for name in own}
    if not _check_display_fields(created):
        return 1
    return 1 if wrong else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare a Zoho CRM for the bot: modules, fields, options.")
    parser.add_argument("--apply", action="store_true", help="create what is missing")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(run(args.apply)))
    except IntegrationError as exc:
        print(f"Zoho refused: {exc.message}")
        sys.exit(2)


if __name__ == "__main__":
    main()
