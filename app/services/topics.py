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


async def topic_breakdown(news_match: dict) -> dict:
    rule_set, approved = await get_effective()
    topics = [t for t in (rule_set or {}).get("topics", []) if t.get("active")]
    patterns = [(t, [_norm(s) for s in t.get("synonyms", []) if s.strip()]) for t in topics]

    stats = {t["code"]: {"code": t["code"], "name": t["name"], "count": 0, "by_media": {}, "high_destaque": 0,
                         "matched_terms": {}, "headlines": []} for t in topics}
    total = unclassified = 0
    cursor = get_db()["news"].find(
        {**news_match, "is_duplicate": False},
        projection={"id": 1, "title": 1, "body": 1, "media_name": 1, "source_destaque": 1, "summary_date": 1},
    ).sort("summary_date", -1).limit(MAX_NEWS)
    async for n in cursor:
        total += 1
        text = _norm(f"{n.get('title', '')} {n.get('body', '')}")
        hit = False
        for t, pats in patterns:
            terms = [p.strip() for p in pats if p in text]
            if not terms:
                continue
            hit = True
            s = stats[t["code"]]
            s["count"] += 1
            media = n.get("media_name") or "Sin resolver"
            s["by_media"][media] = s["by_media"].get(media, 0) + 1
            if n.get("source_destaque") in ("Muy Alto", "Alto"):
                s["high_destaque"] += 1
            for term in terms:
                s["matched_terms"][term] = s["matched_terms"].get(term, 0) + 1
            if len(s["headlines"]) < 8:
                s["headlines"].append({"id": n["id"], "title": n.get("title"), "media": media,
                                       "summary_date": n.get("summary_date"), "destaque": n.get("source_destaque")})
        unclassified += int(not hit)

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
