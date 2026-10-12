"""Serves product photos (public, cache forever — put a CDN in front) and private documents."""
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.security import verify_file_signature
from app.storage import get_store

router = APIRouter(prefix="/files", tags=["Files"])


@router.get("/images/{file_id}")
async def image(file_id: str) -> Response:
    f = await get_store().get(file_id)
    if not f or not f.metadata.get("public"):
        raise HTTPException(404, "Not found")
    return Response(f.data, media_type=f.content_type,
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.get("/private/{file_id}")
async def private_file(file_id: str, exp: int = Query(...), sig: str = Query(...)) -> Response:
    if not verify_file_signature(file_id, exp, sig):
        raise HTTPException(403, "Link expired or invalid")
    f = await get_store().get(file_id)
    if not f:
        raise HTTPException(404, "Not found")
    safe_name = "".join(c for c in f.filename if c.isalnum() or c in "._- ") or "document"
    return Response(f.data, media_type=f.content_type, headers={
        "Cache-Control": "private, no-store",
        "Content-Disposition": f'inline; filename="{safe_name}"',
        "X-Content-Type-Options": "nosniff",
    })
