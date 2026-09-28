"""Browser caching for files whose name changes when their content does.

Dish photos are named by a digest of their source, so a new photo is a new
URL. The browser can therefore keep one for a year and never ask again -
on a slow link the second visit to the menu then downloads almost nothing.
"""

from __future__ import annotations

from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

CACHE_FOREVER = "public, max-age=31536000, immutable"


class ImmutableStaticFiles(StaticFiles):
    """StaticFiles that marks every successful response as cacheable for good."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = CACHE_FOREVER
        return response
