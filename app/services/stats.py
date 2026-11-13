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

    # Evolución diaria (fecha del resumen) con desglose por medio
    by_day: dict[str, dict] = {}
    async for row in await db["news"].aggregate([
        {"$match": news_match},
        {"$group": {"_id": {"d": "$summary_date", "m": "$media_name"}, "n": {"$sum": 1}}},
    ]):
        day = row["_id"]["d"]
        if not day:
            continue
        entry = by_day.setdefault(day, {"date": day, "total": 0, "by_media": {}})
        entry["total"] += row["n"]
        entry["by_media"][row["_id"]["m"] or "Sin resolver"] = row["n"]

    # Temas provisionales (guardados en cada noticia)
    from app.services.rules import get_effective
    rule_set, approved = await get_effective()
    names = {t["code"]: t["name"] for t in (rule_set or {}).get("topics", [])}
    by_topic = []
    async for row in await db["news"].aggregate([
        {"$match": {**news_match, "is_duplicate": False}}, {"$unwind": "$provisional_topics"},
        {"$group": {"_id": "$provisional_topics", "n": {"$sum": 1}}}, {"$sort": {"n": -1}},
    ]):
        by_topic.append({"code": row["_id"], "name": names.get(row["_id"], row["_id"]), "count": row["n"]})
    without_topic = await db["news"].count_documents({**news_match, "is_duplicate": False, "provisional_topics": {"$size": 0}})

    return {
        "by_day": [by_day[k] for k in sorted(by_day)],
        "by_topic": by_topic,
        "without_topic": without_topic,
        "topics_rule_set": (rule_set or {}).get("version"),
        "topics_rule_set_approved": approved,
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
