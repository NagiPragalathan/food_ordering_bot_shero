"""List customers the bot recorded from other teams' WhatsApp numbers. Reads only.

    python -m scripts.report_other_channels

Uses GALLABOX_CHANNEL_ID / WHATSAPP_BUSINESS_NUMBER from .env to know which
channel is the bot's. Numbers are printed with all but the last four digits
hidden. Nothing is changed, here or in Zoho: removing someone is done from the
admin Customers page, and in Zoho by a person (the Zoho record may belong to
another team). See services/channel_report.py.
"""

from __future__ import annotations

import asyncio
import sys

from app.db.session import SessionFactory
from app.integrations.gallabox import channel
from app.services import channel_report


def _masked(number: str) -> str:
    return f"...{number[-4:]}" if number else "?"


async def main() -> int:
    if not channel.our_channel_id():
        print("GALLABOX_CHANNEL_ID is not set, so there is no way to tell channels apart.")
        return 1
    async with SessionFactory() as session:
        report = await channel_report.build(session)

    print(f"Messages logged from other channels: {report.other_messages}")
    print(f"Customers only seen on other channels: {len(report.customers)}\n")
    for f in report.customers:
        c = f.customer
        print(f"  {_masked(c.whatsapp_number):>9}  {(c.name or '-')[:28]:<28}  "
              f"orders={f.orders}  zoho={f.zoho_id or '-':<20}  via {', '.join(sorted(f.channels))}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
