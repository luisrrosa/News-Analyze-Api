import re

from fastapi import APIRouter, Depends, Query, Request

from app.api.compat import news_out
from app.api.dates import summary_date_filter
from app.api.deps import ApiError, AuthContext, require
from app.services.stats import period_stats, summary_dates
from app.services.topics import topic_breakdown
from app.core.audit import write_audit
from app.core.db import get_db, iso_now

router = APIRouter(tags=["news"])


@router.get("/news")
async def list_news(
    page: int = Query(1, ge=1), limit: int = Query(25, ge=1, le=200),
    medio: str | None = None, type: str | None = None, destaque: str | None = None,
    q: str | None = Query(None, max_length=200), is_duplicate: bool | None = None,
    document_id: str | None = None, review_status: str | None = None,
    date_from: str | None = None, date_to: str | None = None,
    ctx: AuthContext = Depends(require("news:read")),
):
    query: dict = summary_date_filter(date_from, date_to)
    if medio and medio != "all":
        query["media_name"] = medio
    if type and type != "all":
        query["content_type"] = type
    if destaque and destaque != "all":
        query["source_destaque"] = destaque
    if is_duplicate is not None:
        query["is_duplicate"] = is_duplicate
    if document_id:
        query["document_id"] = document_id
    if review_status:
        query["review_status"] = review_status
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        query["$or"] = [{"title": rx}, {"body": rx}, {"author_raw": rx}, {"article_section": rx}]
    col = get_db()["news"]
    total = await col.count_documents(query)
    items = [news_out(n) async for n in col.find(query).sort([("summary_date", -1), ("document_id", 1), ("news_index", 1)])
             .skip((page - 1) * limit).limit(limit)]
    return {"items": items, "total": total, "page": page, "limit": limit, "totalPages": -(-total // limit)}


@router.get("/news/summary-dates")
async def list_summary_dates(ctx: AuthContext = Depends(require("news:read"))):
    """Días con datos (para el rango por defecto: último día con datos)."""
    rows = await summary_dates()
    return {"dates": rows, "latest": rows[0]["date"] if rows else None}


@router.get("/news/stats")
async def news_stats(date_from: str | None = None, date_to: str | None = None,
                     ctx: AuthContext = Depends(require("news:read"))):
    f = summary_date_filter(date_from, date_to)
    return {"date_from": date_from, "date_to": date_to, **await period_stats(f, f)}


@router.get("/news/topics")
async def news_topics(date_from: str | None = None, date_to: str | None = None,
                      ctx: AuthContext = Depends(require("news:read"))):
    """Radar: noticias del período por tema del catálogo vigente (clasificación provisional por sinónimos)."""
    return await topic_breakdown(summary_date_filter(date_from, date_to))


@router.get("/news/{news_id}")
async def get_news(news_id: str, ctx: AuthContext = Depends(require("news:read"))):
    n = await get_db()["news"].find_one({"id": news_id})
    if not n:
        raise ApiError(404, "Noticia no encontrada")
    return {"news": news_out(n)}


# ---------------- Mesa de revisión (capa determinista) ----------------

@router.get("/review/queue")
async def review_queue(limit: int = Query(200, ge=1, le=500), date_from: str | None = None, date_to: str | None = None,
                       ctx: AuthContext = Depends(require("news:review"))):
    col = get_db()["news"]
    query = {"review_status": "pending_review", **summary_date_filter(date_from, date_to)}
    items = [news_out(n) async for n in col.find(query).sort("created_at", -1).limit(limit)]
    for i in items:
        i["review_decision"] = None
        i["is_reviewed"] = False
    return {"items": items, "total": await col.count_documents(query),
            "criteria": "Señales de extracción (titular no identificado, medio sin resolver, calidad de texto) y verificación de duplicados."}


@router.post("/review/queue")
async def review_decision(request: Request, ctx: AuthContext = Depends(require("news:approve"))):
    body = await request.json()
    news_id, action, notes = body.get("news_id"), body.get("action"), (body.get("notes") or "").strip()[:2000]
    if not isinstance(news_id, str) or action not in ("approve", "discard"):
        raise ApiError(400, "Parámetros inválidos (news_id y action [approve|discard] requeridos)")
    if action == "discard" and len(notes) < 5:
        raise ApiError(400, "El descarte requiere una justificación")
    news = await get_db()["news"].find_one({"id": news_id}, projection={"review_status": 1})
    if not news:
        raise ApiError(404, "Noticia no encontrada")
    new_status = "approved" if action == "approve" else "rejected"
    now = iso_now()
    # La noticia original no se modifica: solo su estado de revisión; la decisión queda en news_reviews.
    await get_db()["news_reviews"].insert_one({
        "news_id": news_id, "action": new_status.upper(), "previous_status": news["review_status"],
        "reviewer_id": ctx.user["id"], "reviewer_email": ctx.user["email"], "justification": notes, "timestamp_utc": now})
    await get_db()["news"].update_one({"id": news_id}, {"$set": {"review_status": new_status, "updated_at": now}})
    await write_audit(**ctx.actor(), action="NEWS_ITEM_APPROVED" if action == "approve" else "NEWS_ITEM_DISCARDED",
                      entity_type="NEWS_ITEM", entity_id=news_id, status="SUCCESS",
                      details={"decision": new_status, "justification": notes or None})
    return {"success": True, "news_id": news_id, "decision": new_status.upper()}
