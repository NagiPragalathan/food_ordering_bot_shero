"""Authenticated Zoho CRM v6 transport.

Adds the OAuth header to every call and transparently retries once on a 401
(the cached access token expired early, or an admin revoked it).
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.exceptions import IntegrationError
from app.core.logging import get_logger
from app.integrations.base import ApiClient
from app.integrations.zoho.oauth import get_access_token, invalidate_token

log = get_logger(__name__)

API_VERSION = "v6"


class ZohoClient(ApiClient):
    service = "zoho"

    def __init__(self) -> None:
        super().__init__(base_url=settings.zoho_api_url)

    async def _get_client(self):
        """The pooled client, rebuilt if the data centre has changed.

        Connect Zoho saves the data centre while the server runs; a client
        built at import time kept calling zohoapis.com for a .in account,
        and every call came back 401.
        """
        current = settings.zoho_api_url
        if current != self.base_url:
            log.info("zoho_api_host_changed", host=current)
            await self.aclose()
            self._client = None
            self.base_url = current
        return await super()._get_client()

    async def default_headers(self) -> dict[str, str]:
        token = await get_access_token()
        return {
            "Authorization": f"Zoho-oauthtoken {token}",
            "Content-Type": "application/json",
        }

    async def request(self, method: str, url: str, **kw) -> Any:
        try:
            return await super().request(method, url, **kw)
        except IntegrationError as exc:
            if exc.status_code != 401:
                raise
            # Token rejected - force a refresh and make exactly one more attempt.
            log.info("zoho_token_rejected_retrying")
            invalidate_token()
            await get_access_token(force_refresh=True)
            return await super().request(method, url, **kw)

    # -- record helpers -------------------------------------------------------
    def _module_path(self, module: str) -> str:
        return f"/crm/{API_VERSION}/{module}"

    # Shero's CRM has workflows on its modules (kitchen-partner onboarding and
    # the like). Records the bot writes must not set those off, so every
    # write passes an empty trigger list instead of Zoho's default.
    NO_TRIGGERS: list[str] = []

    async def create_record(self, module: str, fields: dict) -> str | None:
        """Insert one record; returns the new Zoho id."""
        body = {"data": [fields], "trigger": self.NO_TRIGGERS}
        result = await self.post(self._module_path(module), json=body)
        return _first_record_id(result)

    async def update_record(self, module: str, record_id: str, fields: dict) -> str | None:
        body = {"data": [{"id": record_id, **fields}], "trigger": self.NO_TRIGGERS}
        result = await self.put(self._module_path(module), json=body)
        return _first_record_id(result)

    async def search(self, module: str, criteria: str) -> list[dict]:
        """COQL-style criteria search, e.g. `(Phone:equals:17325550142)`.

        Zoho answers 204 No Content when nothing matches, which the base client
        turns into None - normalised to an empty list here.
        """
        result = await self.get(
            f"{self._module_path(module)}/search",
            params={"criteria": criteria},
            expected=(200, 204),
        )
        if not result:
            return []
        return result.get("data", []) or []

    async def get_record(self, module: str, record_id: str) -> dict | None:
        result = await self.get(f"{self._module_path(module)}/{record_id}",
                                expected=(200, 204))
        records = (result or {}).get("data") or []
        return records[0] if records else None

    # -- field metadata (scripts/setup_zoho_crm.py) ---------------------------
    async def list_fields(self, module: str) -> list[dict]:
        result = await self.get(f"/crm/{API_VERSION}/settings/fields",
                                params={"module": module})
        return (result or {}).get("fields", []) or []

    async def create_fields(self, module: str, fields: list[dict]) -> list[dict]:
        """Create up to five custom fields (Zoho's per-call limit).

        The create API is v8-only. Returns Zoho's per-field results. Zoho
        answers 207 when some of the batch failed; the failures are raised
        together, naming each field, after the rest were created.
        """
        result = await self.post("/crm/v8/settings/fields", params={"module": module},
                                 json={"fields": fields}, expected=(200, 201, 207))
        rows = (result or {}).get("fields") or []
        failed = []
        for spec, row in zip(fields, rows):
            if row.get("status") == "error":
                failed.append(f"'{spec.get('field_label')}': {row.get('code')} "
                              f"{row.get('message')} {row.get('details')}")
        if failed:
            raise IntegrationError("zoho", "field create failed - " + "; ".join(failed),
                                   payload=rows)
        return rows

    async def add_picklist_options(self, module: str, field_id: str,
                                   options: list[str]) -> None:
        """Add options to a picklist; existing options are kept."""
        body = {"fields": [{"pick_list_values": [
            {"display_value": o, "actual_value": o} for o in options]}]}
        result = await self.request("PATCH", f"/crm/v8/settings/fields/{field_id}",
                                    params={"module": module}, json=body)
        for row in (result or {}).get("fields") or []:
            if row.get("status") == "error":
                raise IntegrationError(
                    "zoho", f"picklist update failed: {row.get('code')} {row.get('message')}",
                    payload=row)

    # -- lead conversion ------------------------------------------------------
    async def convert_lead(self, lead_id: str) -> str:
        """Zoho's Lead -> Contact conversion; returns the new Contact's id.

        Zoho copies the standard fields itself; the bot writes its own
        fields to the Contact straight after (this call's "Contacts" key can
        only name an existing Contact to link to, not set values). No
        Account is created as long as the Lead has no Company. Unlike create
        and update, a conversion cannot skip the CRM's workflows.
        """
        body = {"data": [{"notify_lead_owner": False, "notify_new_entity_owner": False}]}
        result = await self.post(f"{self._module_path('Leads')}/{lead_id}/actions/convert",
                                 json=body)
        records = (result or {}).get("data") or []
        record = records[0] if records else {}
        if not records or record.get("status") == "error":
            raise IntegrationError(
                "zoho", f"lead conversion failed: {record.get('code')} {record.get('message')}",
                payload=record or result)
        # v2 put the ids at the top level, as bare strings; later versions
        # nest them under "details" as objects.
        details = record.get("details") if isinstance(record.get("details"), dict) else record
        contact = details.get("Contacts")
        contact_id = contact.get("id") if isinstance(contact, dict) else contact
        if not contact_id:
            raise IntegrationError("zoho", "lead conversion returned no Contact id",
                                   payload=record)
        return str(contact_id)

    async def delete_record(self, module: str, record_id: str) -> None:
        result = await self.request("DELETE", f"{self._module_path(module)}/{record_id}")
        _first_record_id(result)        # raises on a per-record error

    async def existing_ids(self, module: str, ids: list[str]) -> set[str]:
        """Which of `ids` exist in the module; deleted or converted ones drop out.

        One call per hundred ids, Zoho's limit for the `ids` filter.
        """
        found: set[str] = set()
        for start in range(0, len(ids), 100):
            batch = ids[start:start + 100]
            result = await self.get(self._module_path(module),
                                    params={"ids": ",".join(batch), "fields": "id"},
                                    expected=(200, 204))
            found |= {str(row.get("id")) for row in (result or {}).get("data") or []}
        return found

    async def org_id(self) -> str | None:
        """The org this token belongs to; record ids are only valid there."""
        result = await self.get(f"/crm/{API_VERSION}/org")
        orgs = (result or {}).get("org") or []
        return str(orgs[0]["id"]) if orgs and orgs[0].get("id") else None

    # -- module metadata (scripts/setup_zoho_crm.py) --------------------------
    async def get_module(self, api_name: str) -> dict | None:
        """The module's metadata, or None when there is no such module."""
        result = await self.get(f"/crm/{API_VERSION}/settings/modules/{api_name}",
                                expected=(200, 204, 400, 404))
        modules = (result or {}).get("modules") or []
        return modules[0] if modules else None

    async def create_module(self, api_name: str, singular: str, plural: str) -> None:
        """Create a custom module that every profile can see (v8 API)."""
        profiles = await self.get(f"/crm/{API_VERSION}/settings/profiles")
        body = {"modules": [{
            "api_name": api_name, "singular_label": singular, "plural_label": plural,
            "profiles": [{"id": p["id"]} for p in (profiles or {}).get("profiles") or []],
            "display_field": {"field_label": "Name", "data_type": "text"},
        }]}
        result = await self.post("/crm/v8/settings/modules", json=body,
                                 expected=(200, 201))
        for row in (result or {}).get("modules") or []:
            if row.get("status") == "error":
                raise IntegrationError(
                    "zoho", f"module create failed: {row.get('code')} {row.get('message')} "
                            f"{row.get('details')}", payload=row)



def _first_record_id(result: Any) -> str | None:
    """Pull the record id out of a Zoho write response.

    Zoho reports per-record outcomes inside a 2xx envelope, so a
    `status: error` entry here is a real failure even though the HTTP call
    succeeded.
    """
    records = (result or {}).get("data") or []
    if not records:
        return None
    record = records[0]
    if record.get("status") == "error":
        raise IntegrationError(
            "zoho",
            f"record write failed: {record.get('code')} {record.get('message')}",
            payload=record,
        )
    return (record.get("details") or {}).get("id")


zoho_client = ZohoClient()
