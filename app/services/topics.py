"""Clasificación temática PROVISIONAL por sinónimos del catálogo (Radar de Tendencias, decisión 32).

Determinista y explicable: una noticia pertenece a un tema si su titular o cuerpo contiene alguno de los
sinónimos del tema (frase completa, sin distinguir acentos ni mayúsculas). No es la clasificación institucional
(doc 03 exige evaluar contexto y evidencia): la sustituirá la capa de IA en la Fase 3.
"""
import re
import unicodedata

from app.core.db import get_db
from app.services.rules import get_effective

MAX_NEWS = 20000


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFD", (text or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return " " + re.sub(r"[^a-z0-9ñ]+", " ", t).strip() + " "


def compile_patterns(rule_set: dict | None) -> list[tuple[dict, list[str]]]:
    topics = [t for t in (rule_set or {}).get("topics", []) if t.get("active")]
    return [(t, [_norm(s) for s in t.get("synonyms", []) if s.strip()]) for t in topics]


def match_topics_with_terms(title: str, body: str, patterns: list[tuple[dict, list[str]]]) -> dict[str, list[str]]:
    """Temas que coinciden y los sinónimos que los activaron ({código: [términos]})."""
    text = _norm(f"{title or ''} {body or ''}")
    out: dict[str, list[str]] = {}
    for t, pats in patterns:
        terms = [p.strip() for p in pats if p in text]
        if terms:
            out[t["code"]] = terms[:10]
    return out


def match_topics(title: str, body: str, patterns: list[tuple[dict, list[str]]]) -> list[str]:
    return list(match_topics_with_terms(title, body, patterns))


def _classify_batch(docs: list[dict], patterns) -> list[tuple[str, dict]]:
    return [(d["id"], match_topics_with_terms(d.get("title", ""), d.get("body", ""), patterns)) for d in docs]


async def recompute_news_topics(batch: int = 500) -> int:
    """Recalcula news.provisional_topics (y los términos que los activaron) con la versión efectiva del catálogo.
    Lo ejecuta el worker (tarea con candado), no la API: recorre todas las noticias."""
    import asyncio
    from pymongo import UpdateOne
    rule_set, _ = await get_effective()
    patterns = compile_patterns(rule_set)
    version = (rule_set or {}).get("version")
    col = get_db()["news"]
    n = 0
    docs: list[dict] = []

    async def flush():
        nonlocal n, docs
        results = await asyncio.to_thread(_classify_batch, docs, patterns)
        ops = [UpdateOne({"id": i}, {"$set": {"provisional_topics": sorted(m), "provisional_topic_terms": m,
                                              "provisional_topics_version": version}}) for i, m in results]
        if ops:
            await col.bulk_write(ops, ordered=False)
        n += len(ops)
        docs = []

    async for d in col.find({"topics_manual": {"$ne": True}}, projection={"_id": 0, "id": 1, "title": 1, "body": 1}):
        docs.append(d)
        if len(docs) >= batch:
            await flush()
    if docs:
        await flush()
    return n


async def topic_breakdown(news_match: dict) -> dict:
    """Radar: se agrega en MongoDB sobre los temas ya guardados en cada noticia (no se recalcula el texto)."""
    rule_set, approved = await get_effective()
    topics = [t for t in (rule_set or {}).get("topics", []) if t.get("active")]
    stats = {t["code"]: {"code": t["code"], "name": t["name"], "count": 0, "by_media": {}, "high_destaque": 0,
                         "matched_terms": {}, "headlines": []} for t in topics}
    match = {**news_match, "is_duplicate": False}
    pipeline = [
        {"$match": match},
        {"$facet": {
            "total": [{"$count": "n"}],
            "unclassified": [{"$match": {"$or": [{"provisional_topics": {"$size": 0}}, {"provisional_topics": {"$exists": False}}]}},
                             {"$count": "n"}],
            "by_topic": [
                {"$unwind": "$provisional_topics"},
                {"$group": {"_id": {"t": "$provisional_topics", "m": "$media_name"}, "n": {"$sum": 1},
                            "high": {"$sum": {"$cond": [{"$in": ["$source_destaque", ["Muy Alto", "Alto"]]}, 1, 0]}}}},
            ],
            "terms": [
                {"$project": {"pairs": {"$objectToArray": {"$ifNull": ["$provisional_topic_terms", {}]}}}},
                {"$unwind": "$pairs"}, {"$unwind": "$pairs.v"},
                {"$group": {"_id": {"t": "$pairs.k", "term": "$pairs.v"}, "n": {"$sum": 1}}},
            ],
            "headlines": [
                {"$sort": {"summary_date": -1, "news_index": 1}},
                {"$project": {"id": 1, "title": 1, "media_name": 1, "summary_date": 1, "source_destaque": 1, "provisional_topics": 1}},
                {"$unwind": "$provisional_topics"},
                {"$group": {"_id": "$provisional_topics", "items": {"$push": {
                    "id": "$id", "title": "$title", "media": {"$ifNull": ["$media_name", "Sin resolver"]},
                    "summary_date": "$summary_date", "destaque": "$source_destaque"}}}},
                {"$project": {"items": {"$slice": ["$items", 8]}}},
            ],
        }},
    ]
    rows = [r async for r in await get_db()["news"].aggregate(pipeline, allowDiskUse=True)]
    facet = rows[0] if rows else {}
    total = (facet.get("total") or [{"n": 0}])[0]["n"] if facet.get("total") else 0
    unclassified = (facet.get("unclassified") or [{"n": 0}])[0]["n"] if facet.get("unclassified") else 0
    for r in facet.get("by_topic", []):
        st = stats.get(r["_id"]["t"])
        if st:
            media = r["_id"].get("m") or "Sin resolver"
            st["count"] += r["n"]
            st["high_destaque"] += r["high"]
            st["by_media"][media] = st["by_media"].get(media, 0) + r["n"]
    for r in facet.get("terms", []):
        st = stats.get(r["_id"]["t"])
        if st:
            st["matched_terms"][r["_id"]["term"]] = r["n"]
    for r in facet.get("headlines", []):
        st = stats.get(r["_id"])
        if st:
            st["headlines"] = r["items"]

    ranked = sorted(stats.values(), key=lambda s: -s["count"])
    return {
        "rule_set_version": (rule_set or {}).get("version"),
        "rule_set_approved": approved,
        "method": "Clasificación provisional por sinónimos del catálogo (no institucional)",
        "total_news": total,
        "unclassified": unclassified,
        "topics_with_news": sum(1 for s in ranked if s["count"]),
        "topics_active": len(topics),
        "topics": ranked,
    }
