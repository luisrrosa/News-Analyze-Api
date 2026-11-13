from fastapi import APIRouter, Depends, Query, Request

from app.api.schemas import JustificationIn, NoteIn, ProposeIn, SamplingIn, body
from app.api.compat import news_out
from app.api.dates import summary_date_filter
from app.api.deps import ApiError, AuthContext, require
from app.core.audit import write_audit
from app.services import review as rv

router = APIRouter(prefix="/review", tags=["review"])

STATUSES = {"open", "mine", "pending_review", "in_review", "pending_approval", "corrected", "approved", "rejected"}


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _err(e: rv.ReviewError) -> ApiError:
    return ApiError(e.status, str(e), "REVIEW_ERROR")


@router.get("/queue")
async def get_queue(status: str = "open", reason: str | None = None, date_from: str | None = None,
                    date_to: str | None = None, media: str | None = None, limit: int = Query(200, ge=1, le=500),
                    ctx: AuthContext = Depends(require("news:review"))):
    """Bandeja de revisión por estado y causa, con contadores."""
    if status not in STATUSES:
        raise ApiError(400, "Estado inválido")
    match = summary_date_filter(date_from, date_to)
    if media:
        match["media_name"] = media
    data = await rv.queue(match, status, reason, ctx.user["id"], limit)
    return {
        "items": [news_out(n) for n in data["items"]], "proposals": data["proposals"], "total": data["total"],
        "counts": data["counts"], "reasons": data["reasons"], "field_labels": rv.FIELD_LABELS,
        "editable_fields": sorted(f for f, c in rv.FIELD_CLAIMS.items() if c in ctx.claims),
        "can_approve": "news:approve" in ctx.claims,
    }


@router.get("/settings")
async def get_settings_(ctx: AuthContext = Depends(require("news:review"))):
    return {"sampling_rate": await rv.sampling_rate(), "lock_minutes": rv.LOCK_MINUTES}


@router.put("/settings", openapi_extra=body(SamplingIn))
async def put_settings(request: Request, ctx: AuthContext = Depends(require("review:sampling_manage"))):
    body = await _body(request)
    try:
        try:
            rate = float(body.get("sampling_rate"))
        except (TypeError, ValueError):
            raise ApiError(400, "sampling_rate debe ser un número entre 0 y 1", "VALIDATION_ERROR")
        before, after = await rv.set_sampling_rate(rate, ctx.user["id"])
    except (TypeError, ValueError):
        raise ApiError(400, "Tasa de muestreo inválida")
    except rv.ReviewError as e:
        raise _err(e)
    await write_audit(**ctx.actor(), action="REVIEW_SETTINGS_UPDATED", entity_type="CONFIG", entity_id="review",
                      status="SUCCESS", details={"before": {"sampling_rate": before}, "after": {"sampling_rate": after}})
    return {"sampling_rate": after}


@router.get("/news/{news_id}/history")
async def get_history(news_id: str, ctx: AuthContext = Depends(require("review:history_read"))):
    return {"history": await rv.history(news_id)}


@router.post("/news/{news_id}/take")
async def take(news_id: str, ctx: AuthContext = Depends(require("news:review"))):
    try:
        n = await rv.take(news_id, ctx.user)
    except rv.ReviewError as e:
        raise _err(e)
    await write_audit(**ctx.actor(), action="REVIEW_TAKEN", entity_type="NEWS_ITEM", entity_id=news_id, status="SUCCESS")
    return {"news": news_out(n), "lock_minutes": rv.LOCK_MINUTES}


@router.post("/news/{news_id}/release")
async def release(news_id: str, ctx: AuthContext = Depends(require("news:review"))):
    try:
        await rv.release(news_id, ctx.user)
    except rv.ReviewError as e:
        raise _err(e)
    await write_audit(**ctx.actor(), action="REVIEW_RELEASED", entity_type="NEWS_ITEM", entity_id=news_id, status="SUCCESS")
    return {"success": True}


@router.post("/news/{news_id}/propose", openapi_extra=body(ProposeIn))
async def propose(news_id: str, request: Request, ctx: AuthContext = Depends(require("news:review"))):
    body = await _body(request)
    try:
        r = await rv.propose(news_id, body.get("changes") or {}, str(body.get("justification", "")), ctx.user, ctx.claims)
    except rv.ReviewError as e:
        raise _err(e)
    await write_audit(**ctx.actor(), action="NEWS_CORRECTION_PROPOSED", entity_type="NEWS_ITEM", entity_id=news_id,
                      status="SUCCESS", details={"review_id": r["id"], "fields": [c["field"] for c in r["changes"]],
                                                 "justification": r["justification"]})
    return {"review": r}


@router.post("/news/{news_id}/confirm", openapi_extra=body(JustificationIn))
async def confirm(news_id: str, request: Request, ctx: AuthContext = Depends(require("news:review"))):
    """La extracción es correcta: se aprueba sin cambios."""
    try:
        r = await rv.close_without_changes(news_id, ctx.user, "approved", str((await _body(request)).get("justification", "")))
    except rv.ReviewError as e:
        raise _err(e)
    await write_audit(**ctx.actor(), action="NEWS_ITEM_APPROVED", entity_type="NEWS_ITEM", entity_id=news_id,
                      status="SUCCESS", details={"review_id": r["id"], "reasons": r.get("reasons")})
    return {"review": r}


@router.post("/news/{news_id}/reject", openapi_extra=body(JustificationIn))
async def reject(news_id: str, request: Request, ctx: AuthContext = Depends(require("news:review", "review:reject"))):
    """La pieza no es válida (fragmento, cabecera, repetida…). Se conserva, marcada como rechazada."""
    try:
        r = await rv.close_without_changes(news_id, ctx.user, "rejected", str((await _body(request)).get("justification", "")))
    except rv.ReviewError as e:
        raise _err(e)
    await write_audit(**ctx.actor(), action="NEWS_ITEM_DISCARDED", entity_type="NEWS_ITEM", entity_id=news_id,
                      status="SUCCESS", details={"review_id": r["id"], "justification": r["justification"]})
    return {"review": r}


@router.post("/corrections/{review_id}/approve", openapi_extra=body(NoteIn))
async def approve(review_id: str, request: Request, ctx: AuthContext = Depends(require("news:approve"))):
    try:
        r, before = await rv.approve(review_id, ctx.user, str((await _body(request)).get("note", "")))
    except rv.ReviewError as e:
        raise _err(e)
    await write_audit(**ctx.actor(), action="NEWS_CORRECTION_APPROVED", entity_type="NEWS_ITEM", entity_id=r["news_id"],
                      status="SUCCESS", details={"review_id": review_id, "proposed_by": r["proposed_by_email"],
                                                 "changes": r["changes"]})
    return {"success": True}


@router.post("/corrections/{review_id}/return", openapi_extra=body(NoteIn))
async def return_(review_id: str, request: Request, ctx: AuthContext = Depends(require("news:approve"))):
    try:
        r = await rv.return_proposal(review_id, ctx.user, str((await _body(request)).get("note", "")))
    except rv.ReviewError as e:
        raise _err(e)
    await write_audit(**ctx.actor(), action="NEWS_CORRECTION_RETURNED", entity_type="NEWS_ITEM", entity_id=r["news_id"],
                      status="SUCCESS", details={"review_id": review_id, "proposed_by": r["proposed_by_email"]})
    return {"success": True}
