"""The client's own backend, used when SLOT_SOURCE is 'remote'.

The spec lists a nearby-restaurants API and a slots API as things the client
may already have. With a single kitchen the nearby lookup is no longer needed,
but the slots adapter remains: if the client has a rota system, this is how we
read from it instead of generating slots ourselves.
"""

from __future__ import annotations

from app.core.config import settings
from app.integrations.base import ApiClient


class ClientBackendClient(ApiClient):
    service = "client_backend"

    def __init__(self) -> None:
        super().__init__(base_url=settings.client_backend_base_url)

    async def default_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if settings.client_backend_api_key:
            headers["Authorization"] = f"Bearer {settings.client_backend_api_key}"
        return headers


client_backend = ClientBackendClient()
