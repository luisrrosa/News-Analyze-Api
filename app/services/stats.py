"""Estadísticas agregadas de un período (Resumen General, Radar y Reporte Presidencial).

Se calculan en MongoDB, no en el navegador: escalan a miles de documentos.
"""
from app.core.db import get_db

DESTAQUE_ORDER = ["Muy Alto", "Alto", "Medio", "Bajo"]


async def _group(match: dict, field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    async for row in await get_db()["news"].aggregate([
        {"$match": match}, {"$group": {"_id": f"${field}", "n": {"$sum": 1}}}, {"$sort": {"n": -1}},
    ]):
        out[row["_id"] if row["_id"] is not None else "Sin dato"] = row["n"]
    return out


async def period_stats(news_match: dict, doc_match: dict) -> dict:
    db = get_db()
    total = await db["news"].count_documents(news_match)
    multi = await db["news"].count_documents({**news_match, "pdf_page_indices.1": {"$exists": True}})
    duplicates = await db["news"].count_documents({**news_match, "is_duplicate": True})
    related = await db["news"].count_documents({**news_match, "related_news_ids.0": {"$exists": True}})
    pending_review = await db["news"].count_documents({**news_match, "review_status": "pending_review"})

    documents, pages, dates = 0, 0, set()
    async for d in db["documents"].find({**doc_match, "status": {"$in": ["completed", "completed_with_warnings"]}},
                                        projection={"page_count": 1, "summary_date": 1}):
        documents += 1
        pages += d.get("page_count") or 0
        if d.get("summary_date"):
            dates.add(d["summary_date"])

    by_destaque = await _group(news_match, "source_destaque")
    return {
        "total_news": total,
        "documents": documents,
        "pages": pages,
        "days": sorted(dates),
        "multi_page": multi,
        "duplicates": duplicates,
        "related": related,
        "pending_review": pending_review,
        "by_media": await _group(news_match, "media_name"),
        "by_type": await _group(news_match, "content_type"),
        "by_section": await _group(news_match, "article_section"),
        "by_destaque": {k: by_destaque[k] for k in DESTAQUE_ORDER if k in by_destaque},
        "with_url": await db["news"].count_documents({**news_match, "url": {"$ne": None}}),
    }


async def summary_dates(limit: int = 120) -> list[dict]:
    """Días con resúmenes procesados, del más reciente al más antiguo."""
    rows = []
    async for row in await get_db()["news"].aggregate([
        {"$group": {"_id": "$summary_date", "n": {"$sum": 1}}},
        {"$match": {"_id": {"$ne": None}}},
        {"$sort": {"_id": -1}}, {"$limit": limit},
    ]):
        rows.append({"date": row["_id"], "news": row["n"]})
    return rows
