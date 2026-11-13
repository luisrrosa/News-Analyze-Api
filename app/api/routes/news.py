import csv
import io
import re

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from app.api.compat import news_out
from app.api.dates import summary_date_filter
from app.api.deps import ApiError, AuthContext, require
from app.services.stats import period_stats, summary_dates
from app.services.topics import topic_breakdown
from app.core.audit import write_audit
from app.core.db import get_db, iso_now

router = APIRouter(tags=["news"])


SORTS = {
    "recent": [("summary_date", -1), ("document_id", 1), ("news_index", 1)],
    "oldest": [("summary_date", 1), ("document_id", 1), ("news_index", 1)],
    "destaque": [("destaque_rank", 1), ("summary_date", -1), ("news_index", 1)],
    "media": [("media_name", 1), ("summary_date", -1), ("news_index", 1)],
    "title": [("title", 1)],
}
DESTAQUE_RANK = {"Muy Alto": 0, "Alto": 1, "Medio": 2, "Bajo": 3}


def build_news_filter(
    date_from: str | None = None, date_to: str | None = None, media: str | None = None,
    section: str | None = None, content_type: str | None = None, destaque: str | None = None,
    topic: str | None = None, q: str | None = None, has_url: bool | None = None, multi_page: bool | None = None,
    is_duplicate: bool | None = None, related: bool | None = None, review_status: str | None = None,
    document_id: str | None = None,
) -> dict:
    """Filtro común del explorador, la exportación CSV y los reportes."""
    query: dict = summary_date_filter(date_from, date_to)
    if media:
        query["media_name"] = media
    if section:
        query["article_section"] = section
    if content_type:
        query["content_type"] = content_type
    if destaque:
        query["source_destaque"] = destaque
    if topic == "none":
        query["provisional_topics"] = {"$size": 0}
    elif topic:
        query["provisional_topics"] = topic
    if has_url is not None:
        query["url"] = {"$ne": None} if has_url else None
    if multi_page is not None:
        query["pdf_page_indices.1"] = {"$exists": multi_page}
    if is_duplicate is not None:
        query["is_duplicate"] = is_duplicate
    if related is not None:
        query["related_news_ids.0"] = {"$exists": related}
    if review_status:
        query["review_status"] = review_status
    if document_id:
        query["document_id"] = document_id
    if q:
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        query["$or"] = [{"title": rx}, {"body": rx}, {"author_raw": rx}, {"article_section": rx}]
    return query


def filter_params(
    date_from: str | None = None, date_to: str | None = None, media: str | None = None,
    section: str | None = None, content_type: str | None = None, destaque: str | None = None,
    topic: str | None = None, q: str | None = Query(None, max_length=200), has_url: bool | None = None,
    multi_page: bool | None = None, is_duplicate: bool | None = None, related: bool | None = None,
    review_status: str | None = None, document_id: str | None = None,
) -> dict:
    return build_news_filter(date_from, date_to, media, section, content_type, destaque, topic, q, has_url,
                             multi_page, is_duplicate, related, review_status, document_id)


async def find_news(query: dict, sort: str, skip: int, limit: int) -> list[dict]:
    col = get_db()["news"]
    if sort == "destaque":
        pipeline = [
            {"$match": query},
            {"$addFields": {"destaque_rank": {"$switch": {"branches": [
                {"case": {"$eq": ["$source_destaque", k]}, "then": v} for k, v in DESTAQUE_RANK.items()], "default": 9}}}},
            {"$sort": dict(SORTS["destaque"])}, {"$skip": skip}, {"$limit": limit},
        ]
        return [d async for d in await col.aggregate(pipeline)]
    return [d async for d in col.find(query).sort(SORTS.get(sort, SORTS["recent"])).skip(skip).limit(limit)]


@router.get("/news")
async def list_news(
    page: int = Query(1, ge=1), limit: int = Query(25, ge=1, le=200), sort: str = "recent",
    query: dict = Depends(filter_params), ctx: AuthContext = Depends(require("news:read")),
):
    total = await get_db()["news"].count_documents(query)
    items = [news_out(n) for n in await find_news(query, sort, (page - 1) * limit, limit)]
    return {"items": items, "total": total, "page": page, "limit": limit, "totalPages": -(-total // limit)}


@router.get("/news/facets")
async def news_facets(query: dict = Depends(filter_params), ctx: AuthContext = Depends(require("news:read"))):
    """Valores disponibles para los filtros del explorador (dentro del período)."""
    col = get_db()["news"]
    period = {k: v for k, v in query.items() if k == "summary_date"}
    out = {}
    for field, key in (("media_name", "media"), ("article_section", "section"), ("content_type", "content_type"),
                       ("source_destaque", "destaque"), ("review_status", "review_status")):
        out[key] = sorted(v for v in await col.distinct(field, period) if v)
    return out


CSV_HEADER = ["Fecha del resumen", "Fecha de publicación", "Base de la fecha", "Medio", "Sección", "Tipo", "Destaque",
              "Pág. periódico", "Págs. PDF", "Autor", "Titular", "Texto", "URL", "Temas (provisional)",
              "Duplicada", "Relacionadas", "Estado de revisión", "Documento", "Índice"]


@router.get("/news/export.csv")
async def export_csv(sort: str = "recent", query: dict = Depends(filter_params),
                     ctx: AuthContext = Depends(require("reports:export"))):
    """CSV del listado filtrado: UTF-8 con BOM y punto y coma (Excel en español). Plan V3.0 §16."""
    rows = await find_news(query, sort, 0, 50000)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow(CSV_HEADER)
    for n in rows:
        w.writerow([n.get("summary_date"), n.get("publication_date"), n.get("date_basis"),
                    n.get("media_name") or n.get("media_raw"), n.get("article_section"), n.get("content_type"),
                    n.get("source_destaque"), news_out(n)["source_page_label"],
                    ", ".join(map(str, n.get("pdf_page_indices", []))), n.get("author_raw"), n.get("title"),
                    n.get("body"), n.get("url"), ", ".join(n.get("provisional_topics") or []),
                    "sí" if n.get("is_duplicate") else "no", len(n.get("related_news_ids") or []),
                    n.get("review_status"), n.get("document_id"), n.get("news_index")])
    filters = {k: v for k, v in query.items() if k != "$or"}
    await write_audit(**ctx.actor(), action="REPORT_EXPORTED", entity_type="REPORT", entity_id="news_csv",
                      status="SUCCESS", details={"format": "csv", "rows": len(rows), "filters": str(filters)[:500]})
    return Response(("﻿" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="noticias.csv"', "Cache-Control": "no-store"})


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
    """Ficha de la noticia: datos, documento de origen, temas, relacionadas y duplicadas."""
    db = get_db()
    n = await db["news"].find_one({"id": news_id})
    if not n:
        raise ApiError(404, "Noticia no encontrada")
    proj = {"_id": 0, "id": 1, "title": 1, "media_name": 1, "summary_date": 1, "source_destaque": 1,
            "duplicate_distance": 1, "is_duplicate": 1, "canonical_news_id": 1, "document_id": 1}
    linked_ids = list(n.get("related_news_ids") or [])
    if n.get("canonical_news_id"):
        linked_ids.append(n["canonical_news_id"])
    linked = [d async for d in db["news"].find({"id": {"$in": linked_ids}}, projection=proj)]
    back = [d async for d in db["news"].find(
        {"$or": [{"canonical_news_id": news_id}, {"related_news_ids": news_id}]}, projection=proj)]
    links, seen = [], set()
    for d in linked + back:
        if d["id"] in seen or d["id"] == news_id:
            continue
        seen.add(d["id"])
        if d["id"] == n.get("canonical_news_id"):
            relation = "canonical"
        elif d.get("canonical_news_id") == news_id:
            relation = "duplicate"
        else:
            relation = "related"
        links.append({**d, "relation": relation})
    doc = await db["documents"].find_one({"id": n["document_id"]}, projection={
        "_id": 0, "id": 1, "file_name": 1, "page_count": 1, "summary_date": 1, "document_type": 1})
    from app.services.rules import get_effective
    rule_set, approved = await get_effective()
    names = {t["code"]: t["name"] for t in (rule_set or {}).get("topics", [])}
    topics = [{"code": c, "name": names.get(c, c)} for c in n.get("provisional_topics") or []]
    return {"news": news_out(n), "document": doc, "topics": topics,
            "topics_rule_set": (rule_set or {}).get("version"), "topics_rule_set_approved": approved, "links": links}


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
