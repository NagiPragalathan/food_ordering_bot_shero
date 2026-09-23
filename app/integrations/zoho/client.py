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

    async def create_record(self, module: str, fields: dict) -> str | None:
        """Insert one record; returns the new Zoho id."""
        body = {"data": [fields], "trigger": ["workflow"]}
        result = await self.post(self._module_path(module), json=body)
        return _first_record_id(result)

    async def update_record(self, module: str, record_id: str, fields: dict) -> str | None:
        body = {"data": [{"id": record_id, **fields}], "trigger": ["workflow"]}
        result = await self.put(self._module_path(module), json=body)
        return _first_record_id(result)

    async def upsert_record(self, module: str, fields: dict,
                            duplicate_check_fields: list[str]) -> str | None:
        """Insert-or-update keyed on `duplicate_check_fields`.

        Used for Leads keyed on Phone so a returning WhatsApp number can never
        create a second Lead (spec section 2).
        """
        body = {
            "data": [fields],
            "duplicate_check_fields": duplicate_check_fields,
            "trigger": ["workflow"],
        }
        result = await self.post(f"{self._module_path(module)}/upsert", json=body)
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

    async def convert_lead(self, lead_id: str, *, overwrite: bool = False) -> dict:
        """Convert a Lead into a Contact (spec step 16).

        Returns the created entity ids, e.g. {"Contacts": "...", "Accounts": "..."}.
        Deals are deliberately not created - orders live in the custom Orders
        module instead.
        """
        body = {
            "data": [{
                "overwrite": overwrite,
                "notify_lead_owner": False,
                "notify_new_entity_owner": False,
            }]
        }
        result = await self.post(
            f"{self._module_path('Leads')}/{lead_id}/actions/convert", json=body
        )
        records = (result or {}).get("data") or []
        return records[0] if records else {}


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
