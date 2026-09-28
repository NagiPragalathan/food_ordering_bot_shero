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

        The create API is v8-only. Returns Zoho's per-field results; a
        failed field is raised, not skipped.
        """
        result = await self.post("/crm/v8/settings/fields", params={"module": module},
                                 json={"fields": fields}, expected=(200, 201))
        rows = (result or {}).get("fields") or []
        for row in rows:
            if row.get("status") == "error":
                raise IntegrationError(
                    "zoho", f"field create failed: {row.get('code')} {row.get('message')} "
                            f"{row.get('details')}", payload=row)
        return rows

    async def add_picklist_option(self, module: str, field_id: str, option: str) -> None:
        """Add one option to a custom picklist; existing options are kept."""
        body = {"fields": [{"pick_list_values": [
            {"display_value": option, "actual_value": option}]}]}
        result = await self.request("PATCH", f"/crm/v8/settings/fields/{field_id}",
                                    params={"module": module}, json=body)
        for row in (result or {}).get("fields") or []:
            if row.get("status") == "error":
                raise IntegrationError(
                    "zoho", f"picklist update failed: {row.get('code')} {row.get('message')}",
                    payload=row)


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
