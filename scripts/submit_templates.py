"""Show the WhatsApp templates, or create them on the WABA for approval.

    python -m scripts.submit_templates             # show them, change nothing
    python -m scripts.submit_templates --status    # what exists on the channel
    python -m scripts.submit_templates --json      # the component payloads
    python -m scripts.submit_templates --submit    # create the missing ones

`--submit` goes through Gallabox, which forwards each template to Meta and
needs only `GALLABOX_ACCOUNT_ID` plus the API key the bot already sends with.
`--submit --via-meta` uses the Graph API instead, for a WABA where you hold a
system-user token (`META_SYSTEM_USER_TOKEN`, `META_WABA_ID`).

Submitting is safe to repeat: templates that already exist on the channel are
skipped rather than duplicated.

**Templates are not needed to test the conversation.** A business may send
free-form messages for 24 hours after a customer writes in, and the whole
ordering flow happens inside that window. Templates matter for the messages we
*start*: the welcome opener, the payment reminder, the expiry notice, delivery
updates and the feedback request.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx

from app.core.config import is_unset, settings
from app.core.exceptions import ConfigurationError, IntegrationError
from app.integrations.gallabox import template_admin as admin
from app.integrations.gallabox import templates as tpl

GRAPH = "https://graph.facebook.com/v21.0"


def payload(spec: tpl.TemplateSpec) -> dict:
    """The Graph API body. Gallabox takes the same `components` array."""
    return {
        "name": spec.name,
        "language": "en",
        "category": spec.category,
        "components": admin.components(spec),
    }


def show() -> None:
    for index, spec in enumerate(tpl.MANAGED_TEMPLATES, start=1):
        print(f"\n{'=' * 72}\n{index}. {spec.name}   [{spec.category}]")
        print(f"   when: {spec.trigger}")
        print("\n   body:\n     " + spec.sample_body.replace("\n", "\n     "))
        if spec.params:
            print("\n   parameters, in order:")
            for number, name in enumerate(spec.params, start=1):
                print(f"     {{{{{number}}}}}  {name}  e.g. {admin.example_for(name)}")
        if spec.button_kind == "dynamic_url":
            base = settings.pay_redirect_base_url.rstrip("/")
            print(f"\n   button: URL '{spec.button_label}' -> {base}/{{{{1}}}}")
        elif spec.button_kind == "quick_reply":
            print(f"\n   buttons: {', '.join(spec.quick_replies)}")


async def status() -> int:
    """What is on the sending channel right now, against what we expect."""
    try:
        live = {row.get("name"): row for row in await admin.list_templates()}
    except (ConfigurationError, IntegrationError) as exc:
        print(f"Could not read the templates: {exc}")
        return 1

    print(f"channel {settings.gallabox_channel_id}\n")
    for spec in tpl.MANAGED_TEMPLATES:
        row = live.get(spec.name)
        if row is None:
            print(f"  missing    {spec.name}")
            continue
        state = row.get("status", "?")
        reason = row.get("rejected_reason")
        suffix = f"  ({reason})" if reason and reason != "NONE" else ""
        print(f"  {state:<10} {spec.name}{suffix}")

    extra = set(live) - {s.name for s in tpl.MANAGED_TEMPLATES}
    for name in sorted(extra):
        print(f"  (not ours) {name}")
    return 0


async def submit_via_gallabox() -> int:
    """Create every template the channel does not already have."""
    try:
        existing = {row.get("name"): row for row in await admin.list_templates()}
    except (ConfigurationError, IntegrationError) as exc:
        print(f"Could not read the existing templates: {exc}")
        return 1

    failures = 0
    stuck: list[str] = []
    for spec in tpl.MANAGED_TEMPLATES:
        row = existing.get(spec.name)
        if row is not None:
            state = row.get("status", "?")
            print(f"  {state:<10} {spec.name}")
            # The dev API cannot edit or delete, so a failed template holds its
            # name until somebody removes it in the dashboard.
            if state in ("error", "rejected"):
                stuck.append(f"{spec.name}: {row.get('err') or row.get('rejected_reason')}")
            continue
        try:
            record = await admin.create_template(spec)
        except (ConfigurationError, IntegrationError) as exc:
            print(f"  FAILED     {spec.name}: {exc}")
            failures += 1
            continue
        print(f"  submitted  {spec.name}  "
              f"status={record.get('status', '?')} "
              f"id={record.get('whatsappTemplateId', '?')}")

    if stuck:
        print("\nThese failed and keep their name until you delete them in "
              "Gallabox -> Templates, then re-run --submit:")
        for line in stuck:
            print(f"  {line}")

    _approval_note(failures)
    return 1 if failures else 0


async def submit_via_meta() -> int:
    token = settings.meta_system_user_token
    waba = getattr(settings, "meta_waba_id", "")

    if is_unset(token) or is_unset(waba):
        print("Cannot submit through Meta: META_SYSTEM_USER_TOKEN and "
              "META_WABA_ID are both required.\nDrop --via-meta to submit "
              "through Gallabox instead, which needs neither.")
        return 1

    failures = 0
    async with httpx.AsyncClient(timeout=30) as client:
        for spec in tpl.MANAGED_TEMPLATES:
            try:
                response = await client.post(
                    f"{GRAPH}/{waba}/message_templates",
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload(spec),
                )
            except httpx.HTTPError as exc:
                print(f"  FAILED     {spec.name}: network error: {exc}")
                failures += 1
                continue

            body = response.json() if response.content else {}
            if response.status_code < 300:
                print(f"  submitted  {spec.name}  id={body.get('id', '?')}")
            else:
                error = (body.get("error") or {}).get("message", response.text[:200])
                print(f"  FAILED     {spec.name}: {error}")
                failures += 1

    _approval_note(failures)
    return 1 if failures else 0


def _approval_note(failures: int) -> None:
    print(f"\n{failures} failed." if failures else "\nAll done.")
    print("Approval usually takes minutes to a few hours. Check progress with "
          "--status, or in Gallabox -> Templates.")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="print the component payloads instead of prose")
    parser.add_argument("--status", action="store_true",
                        help="show what exists on the sending channel")
    parser.add_argument("--submit", action="store_true",
                        help="create the templates that do not exist yet")
    parser.add_argument("--via-meta", action="store_true",
                        help="submit through the Graph API rather than Gallabox")
    args = parser.parse_args(argv)

    if args.status:
        return asyncio.run(status())

    if args.submit:
        return asyncio.run(submit_via_meta() if args.via_meta
                           else submit_via_gallabox())

    if args.json:
        print(json.dumps([payload(s) for s in tpl.MANAGED_TEMPLATES], indent=2))
        return 0

    show()
    print(f"\n{'=' * 72}")
    print("These are definitions only - nothing was sent.")
    print("To create them on the WABA:  python -m scripts.submit_templates --submit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
