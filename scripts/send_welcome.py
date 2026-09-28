"""Send the welcome opener to one or more WhatsApp numbers.

    python -m scripts.send_welcome 917401268091
    python -m scripts.send_welcome 917401268091 14155550123

This is how you reach somebody who has never written to us. It creates the
lead if there is none, greets them as "there" when we have no name, and the
Order Now button drops them into onboarding, which asks for the name.

`shero_welcome` must be approved on the sending channel first - check with
`python -m scripts.submit_templates --status`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.exceptions import ConfigurationError, IntegrationError
from app.db.session import session_scope
from app.services.outreach import AlreadyWithAgent, NotAllowlisted, send_welcome


async def run(numbers: list[str]) -> int:
    failures = 0
    for number in numbers:
        try:
            async with session_scope() as session:
                customer, created = await send_welcome(session, number)
        except (AlreadyWithAgent, NotAllowlisted) as exc:
            print(f"  skipped    {number}: {exc}")
            continue
        except (ConfigurationError, IntegrationError) as exc:
            print(f"  FAILED     {number}: {exc}")
            failures += 1
            continue

        who = customer.name or "no name yet"
        origin = "new lead" if created else "existing lead"
        print(f"  sent       {customer.whatsapp_number}  ({origin}, {who})")

    if failures:
        print(f"\n{failures} of {len(numbers)} could not be sent.")
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("numbers", nargs="+",
                        help="WhatsApp numbers in international form, no +")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.numbers))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
