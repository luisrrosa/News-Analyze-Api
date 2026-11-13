from fastapi import APIRouter, Depends, Request

from app.api.deps import ApiError, AuthContext, require, require_any
from app.core.audit import write_audit
from app.services import catalogs

router = APIRouter(prefix="/catalogs", tags=["catalogs"])


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@router.get("/media")
async def get_media(ctx: AuthContext = Depends(require_any("catalogs:read", "news:read", "news:review", "reports:export"))):
    """Lista de medios: la usan los filtros del explorador, el visor, la revisión y los reportes."""
    return {"media": await catalogs.list_media()}


@router.post("/media", status_code=201)
async def create_media(request: Request, ctx: AuthContext = Depends(require("catalogs:media_manage"))):
    try:
        _, after = await catalogs.save_media(None, await _body(request))
    except catalogs.CatalogError as e:
        raise ApiError(400, str(e), "CATALOG_ERROR")
    await write_audit(**ctx.actor(), action="CATALOG_MEDIA_CREATED", entity_type="MEDIA", entity_id=after["id"],
                      status="SUCCESS", details={"after": after})
    return {"media": after}


@router.put("/media/{media_id}")
async def update_media(media_id: str, request: Request, ctx: AuthContext = Depends(require("catalogs:media_manage"))):
    try:
        before, after = await catalogs.save_media(media_id, await _body(request))
    except catalogs.CatalogError as e:
        raise ApiError(400, str(e), "CATALOG_ERROR")
    await write_audit(**ctx.actor(), action="CATALOG_MEDIA_UPDATED", entity_type="MEDIA", entity_id=media_id,
                      status="SUCCESS", details={"before": before, "after": after})
    return {"media": after}


@router.get("/officeholders")
async def get_officeholders(ctx: AuthContext = Depends(require("catalogs:read"))):
    return {"officeholders": await catalogs.list_officeholders(), "state_powers": catalogs.STATE_POWERS}


@router.post("/officeholders", status_code=201)
async def create_officeholder(request: Request, ctx: AuthContext = Depends(require("catalogs:officeholders_manage"))):
    try:
        _, after = await catalogs.save_officeholder(None, await _body(request), ctx.user["id"])
    except catalogs.CatalogError as e:
        raise ApiError(400, str(e), "CATALOG_ERROR")
    await write_audit(**ctx.actor(), action="CATALOG_OFFICEHOLDER_CREATED", entity_type="OFFICEHOLDER",
                      entity_id=after["id"], status="SUCCESS", details={"after": after})
    return {"officeholder": after}


@router.put("/officeholders/{oid}")
async def update_officeholder(oid: str, request: Request, ctx: AuthContext = Depends(require("catalogs:officeholders_manage"))):
    try:
        before, after = await catalogs.save_officeholder(oid, await _body(request), ctx.user["id"])
    except catalogs.CatalogError as e:
        raise ApiError(400, str(e), "CATALOG_ERROR")
    await write_audit(**ctx.actor(), action="CATALOG_OFFICEHOLDER_UPDATED", entity_type="OFFICEHOLDER", entity_id=oid,
                      status="SUCCESS", details={"before": before, "after": after})
    return {"officeholder": after}
