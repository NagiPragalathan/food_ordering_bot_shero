"""Report which credentials are still missing, for each business
(Shero US, DST India).

    python -m scripts.check_config

Exits non-zero when anything required is blank, so it can gate a deploy.
Prints names only - never values.
"""

from __future__ import annotations

import sys

from app.core import tenants
from app.core.config import SHARED_FIELDS, settings

# Optional settings worth flagging, with what you lose without them.
OPTIONAL_HINTS = {
    "GOOGLE_MAPS_API_KEY": (
        "ZIP geocoding falls back to OpenStreetMap Nominatim "
        "(free, ~1 request/second, no SLA)"
    ),
    "CLIENT_BACKEND_BASE_URL": (
        "only needed if OUTLET_SOURCE or SLOT_SOURCE is set to 'remote'"
    ),
}


def env_name(tenant, name: str) -> str:
    """The .env variable a setting is read from for `tenant`: DST India's own
    ones carry DST_, the shared ones (one Gallabox account, one sign-in) not."""
    if tenant.is_default or name.lower() in SHARED_FIELDS:
        return name
    return f"{tenant.env_prefix}{name}"


def report(tenant) -> list[str]:
    print(f"=== {tenant.name} " + "=" * max(3, 60 - len(tenant.name)))
    print(f"Environment : {settings.app_env}")
    print(f"Database    : {'set' if settings.database_url.strip() else 'NOT SET'}")
    print(f"Public URL  : {settings.public_base_url}")
    print(f"Pay redirect: {settings.pay_redirect_base_url}")
    print(f"WhatsApp    : channel {settings.gallabox_channel_id or '(not set)'}")
    print(f"Payments    : {settings.payment_provider}   Delivery: {settings.delivery_provider}")
    print(f"Money       : {settings.currency} ({settings.currency_symbol})   "
          f"Distances: {settings.distance_unit}   Postal: {settings.postal_code_label}")
    print(f"Zoho DC     : {settings.zoho_data_center} -> {settings.zoho_api_url}")
    print(f"Outlets     : {settings.outlet_source}")
    print(f"Slots       : {settings.slot_source}")
    print(f"Scheduler   : {'on' if settings.enable_scheduler else 'off'}")
    print()

    missing = [env_name(tenant, name) for name in settings.missing_credentials()]
    if missing:
        print(f"MISSING ({len(missing)} required):")
        for name in missing:
            print(f"  - {name}")
    else:
        print("All required credentials are set.")

    optional_missing = []
    if not settings.google_maps_api_key:
        optional_missing.append("GOOGLE_MAPS_API_KEY")
    if settings.outlet_source == "remote" or settings.slot_source == "remote":
        if not settings.client_backend_base_url:
            optional_missing.append("CLIENT_BACKEND_BASE_URL")

    if optional_missing:
        print("\nOptional / conditional:")
        for name in optional_missing:
            print(f"  - {name}: {OPTIONAL_HINTS.get(name, '')}")
    print()
    return missing


def main() -> int:
    """Every business set up on this server (app/core/tenants.py)."""
    # A Windows console cannot print the rupee sign; never crash over it.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    missing: list[str] = []
    for tenant in tenants.ALL:
        with tenants.use(tenant):
            if not settings.database_url.strip():
                print(f"=== {tenant.name}: not set up (no {tenant.env_prefix}DATABASE_URL)\n")
                continue
            missing += report(tenant)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
