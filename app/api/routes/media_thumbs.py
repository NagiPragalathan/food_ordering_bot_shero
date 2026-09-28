"""Card-sized dish photos: GET /media/thumb/{name}.

Registered before the /media static mount, which would otherwise claim the
path. Files are named by content digest, so a changed photo gets a new name
and the response can be cached for good.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from app.core.http_cache import CACHE_FOREVER
from app.services import media_thumbs

router = APIRouter(tags=["media"])


@router.get(f"{media_thumbs.THUMB_URL_PREFIX}/{{name}}", include_in_schema=False)
async def thumbnail(name: str) -> FileResponse:
    path = await asyncio.to_thread(media_thumbs.thumbnail_file, name)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such photo")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": CACHE_FOREVER})
