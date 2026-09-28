"""Add the fields the bot writes to Shero's Zoho CRM (docs/zoho-setup.md).

    python -m scripts.setup_zoho_crm           # dry run: list what is missing
    python -m scripts.setup_zoho_crm --apply   # create it

Only missing fields are created, so it is safe to run again. Nothing is
renamed or deleted. After creating, it reads the modules back and checks
each field got the API name the bot uses (fields.py).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.config import settings
from app.core.exceptions import IntegrationError
from app.db.session import SessionFactory
from app.integrations.zoho import schema
from app.integrations.zoho.client import zoho_client
from app.services.settings_store import apply_overrides

BATCH = 5   # Zoho creates at most five fields per call


def _module(name: str) -> str:
    """schema.py says "Orders"; the live module name is configurable."""
    return settings.zoho_orders_module if name == "Orders" else name


async def _fields_by_module() -> dict[str, dict[str, dict]]:
    modules = {_module(spec.module) for spec in schema.FIELDS}
    modules |= {_module(m) for m, _, _ in schema.PICKLIST_OPTIONS}
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


async def run(apply: bool) -> int:
    async with SessionFactory() as session:
        await apply_overrides(session)      # the refresh token lives in the DB

    live = await _fields_by_module()
    existing = {m: set(rows) for m, rows in live.items()}
    missing = [s for s in schema.FIELDS
               if s.api_name not in existing.get(_module(s.module), set())]
    options = _missing_options(live)

    print(f"Zoho data centre: {settings.zoho_data_center}")
    if not missing and not options:
        print("Nothing to do - every field and option is already there.")
        return 0
    for spec in missing:
        print(f"  + field  {_module(spec.module)}.{spec.api_name} "
              f"({spec.body['data_type']}, label '{spec.body['field_label']}')")
    for module, field, option in options:
        print(f"  + option '{option}' on {module}.{field['api_name']}")

    if not apply:
        print("\nDry run. Re-run with --apply to create these.")
        return 0

    for module in sorted({_module(s.module) for s in missing}):
        specs = [s for s in missing if _module(s.module) == module]
        for start in range(0, len(specs), BATCH):
            batch = specs[start:start + BATCH]
            await zoho_client.create_fields(module, [s.body for s in batch])
            print(f"created {len(batch)} field(s) on {module}")
    for module, field, option in options:
        await zoho_client.add_picklist_option(module, field["id"], option)
        print(f"added '{option}' to {module}.{field['api_name']}")

    # Zoho names each field from its label; make sure it matches fields.py.
    after = {m: set(rows) for m, rows in (await _fields_by_module()).items()}
    wrong = [s for s in schema.FIELDS if s.api_name not in after.get(_module(s.module), set())]
    for spec in wrong:
        print(f"  !! {_module(spec.module)}: expected API name {spec.api_name} "
              f"for '{spec.body['field_label']}' - update fields.py to match Zoho")
    return 1 if wrong else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add the fields the bot writes to Shero's Zoho CRM.")
    parser.add_argument("--apply", action="store_true", help="create what is missing")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(run(args.apply)))
    except IntegrationError as exc:
        print(f"Zoho refused: {exc.message}")
        sys.exit(2)


if __name__ == "__main__":
    main()
