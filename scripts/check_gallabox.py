"""Verify the Gallabox credentials before relying on them.

    python -m scripts.check_gallabox                   # offline format checks
    python -m scripts.check_gallabox --send 17325550142  # real end-to-end test

The offline pass catches the common mistakes (blank value, secret pasted into
the key field, a channel ID that is not an ObjectId) without touching the
network.

`--send` is the only test that proves all three values work *together*: it
sends a real WhatsApp text through the real channel. Note the 24-hour rule -
a free-form text only reaches someone who has messaged the business number in
the last 24 hours, so message the business from that phone first, then run
this straight away.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys

import httpx

from app.core.config import settings

# Gallabox ids are MongoDB ObjectIds: 24 lowercase hex characters,
# e.g. 647062b51e3c77d2741188cb
OBJECT_ID_RE = re.compile(r"^[0-9a-f]{24}$")


def check_format() -> list[str]:
    """Local sanity checks. Returns a list of problems."""
    problems: list[str] = []

    key = settings.gallabox_api_key.strip()
    secret = settings.gallabox_api_secret.strip()
    channel = settings.gallabox_channel_id.strip()

    if not key:
        problems.append("GALLABOX_API_KEY is empty")
    if not secret:
        problems.append("GALLABOX_API_SECRET is empty")
    if not channel:
        problems.append("GALLABOX_CHANNEL_ID is empty")

    if key and secret and key == secret:
        problems.append(
            "GALLABOX_API_KEY and GALLABOX_API_SECRET are identical - "
            "the same value was probably pasted twice"
        )

    # The API key is also 24 hex characters, so the two are easy to swap.
    if channel and key and channel.lower() == key.lower():
        problems.append(
            "GALLABOX_CHANNEL_ID is the same as GALLABOX_API_KEY - both are "
            "24 hex characters, so the API key was probably pasted into the "
            "channel field. The channel ID comes from "
            "Settings -> WhatsApp Channel -> Channel Id."
        )
    elif channel and not OBJECT_ID_RE.match(channel.lower()):
        problems.append(
            f"GALLABOX_CHANNEL_ID '{channel}' does not look like a Gallabox id "
            "(expected 24 hex characters, e.g. 647062b51e3c77d2741188cb). "
            "A phone number is not the channel ID."
        )

    if not settings.gallabox_webhook_token:
        problems.append(
            "GALLABOX_WEBHOOK_TOKEN is empty - generate one "
            "(openssl rand -hex 32) and paste the same value into the "
            "Gallabox webhook configuration"
        )

    return problems


async def send_test_message(to: str) -> int:
    """Send a real WhatsApp text. Returns a process exit code."""
    from app.integrations.gallabox.client import normalise_phone

    number = normalise_phone(to)
    if not number:
        print(f"'{to}' is not a usable phone number", file=sys.stderr)
        return 2

    payload = {
        "channelId": settings.gallabox_channel_id,
        "channelType": "whatsapp",
        "recipient": {"phone": number},
        "whatsapp": {
            "type": "text",
            "text": {"body": "Shero bot credential test - you can ignore this."},
        },
    }
    headers = {
        "apiKey": settings.gallabox_api_key,
        "apiSecret": settings.gallabox_api_secret,
        "Content-Type": "application/json",
    }
    url = f"{settings.gallabox_base_url.rstrip('/')}/messages/whatsapp"

    print(f"POST {url}")
    print(f"  channelId {settings.gallabox_channel_id}")
    print(f"  to        ***{number[-4:]}")

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        print(f"\nNetwork error: {exc}", file=sys.stderr)
        return 1

    print(f"\nHTTP {response.status_code}")
    print(response.text[:1000])

    if response.status_code in (200, 201, 202):
        print("\nCredentials work. Check that phone for the message.")
        print("If the call succeeded but nothing arrived, it is almost always "
              "the 24-hour window - message the business number from that "
              "phone first, then re-run.")
        return 0

    if response.status_code in (401, 403):
        print("\nRejected: the API key or secret is wrong, or the key lacks "
              "permission to send messages.", file=sys.stderr)
    elif response.status_code == 404:
        print("\nNot found: usually a wrong channelId, or this plan does not "
              "expose the messages API.", file=sys.stderr)
    else:
        print("\nSend failed - see the response body above.", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Gallabox credentials.")
    parser.add_argument(
        "--send", metavar="PHONE",
        help="send a real WhatsApp test message to this number (digits, with "
             "country code) to prove all three values work together",
    )
    args = parser.parse_args()

    print("Gallabox credential check")
    print(f"  base URL   {settings.gallabox_base_url}")
    print(f"  API key    {_masked(settings.gallabox_api_key)}")
    print(f"  API secret {_masked(settings.gallabox_api_secret)}")
    print(f"  channel ID {settings.gallabox_channel_id or '(not set)'}")
    print()

    problems = check_format()
    if problems:
        print(f"{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("Format checks passed.")
    if not args.send:
        print("\nRun with --send <phone> to prove the credentials actually work.")
        return 0

    print()
    return asyncio.run(send_test_message(args.send))


def _masked(value: str) -> str:
    """Show only enough to tell two values apart."""
    value = (value or "").strip()
    if not value:
        return "(not set)"
    return f"{value[:4]}...{value[-4:]} ({len(value)} chars)"


if __name__ == "__main__":
    sys.exit(main())
