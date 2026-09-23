"""Report which credentials are still missing.

    python -m scripts.check_config

Exits non-zero when anything required is blank, so it can gate a deploy.
Prints names only - never values.
"""

from __future__ import annotations

import sys

from app.core.config import settings

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


def main() -> int:
    print(f"Environment : {settings.app_env}")
    print(f"Public URL  : {settings.public_base_url}")
    print(f"Pay redirect: {settings.pay_redirect_base_url}")
    print(f"Zoho DC     : {settings.zoho_data_center} -> {settings.zoho_api_url}")
    print(f"Outlets     : {settings.outlet_source}")
    print(f"Slots       : {settings.slot_source}")
    print(f"Scheduler   : {'on' if settings.enable_scheduler else 'off'}")
    print()

    missing = settings.missing_credentials()
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

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
