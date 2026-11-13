"""Revisión humana (Fase 4, Plan V3.0 §11.6; doc 01 §8).

Flujo de una noticia (news.review_status):
  not_required ─(señal o muestreo)→ pending_review ─tomar→ in_review ─proponer→ pending_approval
       ↑                                   ↑                    │                     │
       │                                   └──── liberar/vence ─┘     devolver ───────┤ (otro revisor)
       │                                                                              ↓
       │                           approved (sin cambios)   rejected (pieza no válida)   corrected (aprobada)
- Doble control: quien propone una corrección no puede aprobarla (claim news:approve).
- Cada grupo de campos exige su propio claim (review:edit_*).
- El original nunca se pierde: antes de la primera corrección aprobada se copia a original_extraction;
  cada propuesta queda en news_reviews con antes/después, autor, fecha y justificación.
"""
import random
import uuid
from datetime import date, timedelta

from app.core.db import get_db, iso_now, utcnow

LOCK_MINUTES = 30
DEFAULT_SAMPLING_RATE = 0.05
OPEN_STATUSES = ("pending_review", "in_review")
REASON_LABELS = {
    "title_not_found": "Titular no identificado",
    "media_unresolved": "Medio sin resolver",
    "text_quality_failed": "Calidad de texto insuficiente",
    "duplicate_link_verification": "Verificar vínculo de duplicado",
    "quality_sample": "Muestreo de control de calidad",
}

# Campo → (grupo/claim, validador)
CONTENT_TYPES = {"Noticia", "Artículo", "Editorial"}
DESTAQUES = {"Muy Alto", "Alto", "Medio", "Bajo"}
FIELD_CLAIMS = {
    "title": "review:edit_metadata", "media_name": "review:edit_metadata", "article_section": "review:edit_metadata",
    "content_type": "review:edit_metadata", "source_destaque": "review:edit_metadata", "author_raw": "review:edit_metadata",
    "source_page_number": "review:edit_metadata", "source_page_to": "review:edit_metadata",
    "publication_date": "review:edit_dates",
    "body": "review:edit_text",
    "provisional_topics": "review:edit_topics",
    "canonical_news_id": "review:edit_links", "related_news_ids": "review:edit_links",
}
FIELD_LABELS = {
    "title": "Titular", "media_name": "Medio", "article_section": "Sección", "content_type": "Tipo",
    "source_destaque": "Destaque", "author_raw": "Autor", "source_page_number": "Página del periódico",
    "source_page_to": "Página final del periódico", "publication_date": "Fecha de publicación", "body": "Texto",
    "provisional_topics": "Temas", "canonical_news_id": "Noticia original (duplicado de)",
    "related_news_ids": "Noticias relacionadas",
}


class ReviewError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------- Muestreo ----------------
async def sampling_rate() -> float:
    cfg = await get_db()["system_configs"].find_one({"key": "review"})
    try:
        return max(0.0, min(1.0, float((cfg or {}).get("sampling_rate", DEFAULT_SAMPLING_RATE))))
    except (TypeError, ValueError):
        return DEFAULT_SAMPLING_RATE


async def set_sampling_rate(rate: float, user_id: str) -> tuple[float, float]:
    if not 0 <= rate <= 1:
        raise ReviewError("La tasa de muestreo debe estar entre 0 y 1")
    before = await sampling_rate()
    await get_db()["system_configs"].update_one(
        {"key": "review"}, {"$set": {"key": "review", "sampling_rate": rate, "updated_by": user_id, "updated_at": iso_now()}},
        upsert=True)
    return before, rate


def apply_sampling(review_status: str, reasons: list[str], rate: float, rng: random.Random | None = None) -> tuple[str, list[str]]:
    """Durante la ingesta: una noticia sin señales entra a revisión con probabilidad `rate`."""
    if review_status == "not_required" and rate > 0 and (rng or random).random() < rate:
        return "pending_review", reasons + ["quality_sample"]
    return review_status, reasons


# ---------------- Bloqueo ("tomar la noticia") ----------------
async def _expire_locks() -> None:
    await get_db()["news"].update_many(
        {"review_status": "in_review", "review_locked_until": {"$lt": utcnow()}},
        {"$set": {"review_status": "pending_review", "review_assigned_to": None, "review_locked_until": None}})


async def take(news_id: str, user: dict) -> dict:
    await _expire_locks()
    col = get_db()["news"]
    until = utcnow() + timedelta(minutes=LOCK_MINUTES)
    doc = await col.find_one_and_update(
        {"id": news_id, "$or": [{"review_status": "pending_review"},
                                {"review_status": "in_review", "review_assigned_to": user["id"]}]},
        {"$set": {"review_status": "in_review", "review_assigned_to": user["id"],
                  "review_assigned_email": user["email"], "review_locked_until": until}},
        return_document=True)
    if doc:
        return doc
    current = await col.find_one({"id": news_id}, projection={"review_status": 1, "review_assigned_email": 1})
    if not current:
        raise ReviewError("Noticia no encontrada", 404)
    if current["review_status"] == "in_review":
        raise ReviewError(f"La está revisando {current.get('review_assigned_email')}", 409)
    raise ReviewError("La noticia no está pendiente de revisión", 409)


async def release(news_id: str, user: dict) -> None:
    res = await get_db()["news"].update_one(
        {"id": news_id, "review_status": "in_review", "review_assigned_to": user["id"]},
        {"$set": {"review_status": "pending_review", "review_assigned_to": None, "review_locked_until": None}})
    if not res.modified_count:
        raise ReviewError("No tienes tomada esta noticia", 409)


async def _require_lock(news_id: str, user: dict) -> dict:
    await _expire_locks()
    n = await get_db()["news"].find_one({"id": news_id})
    if not n:
        raise ReviewError("Noticia no encontrada", 404)
    if n.get("review_status") != "in_review" or n.get("review_assigned_to") != user["id"]:
        raise ReviewError("Primero debe tomar la noticia para revisarla", 409)
    return n


# ---------------- Validación de cambios ----------------
async def _validate_changes(news: dict, changes: dict, claims: list[str]) -> list[dict]:
    if not isinstance(changes, dict) or not changes:
        raise ReviewError("No hay cambios que proponer")
    db = get_db()
    out = []
    for field, value in changes.items():
        if field not in FIELD_CLAIMS:
            raise ReviewError(f"El campo «{field}» no se puede corregir")
        if FIELD_CLAIMS[field] not in claims:
            raise ReviewError(f"No tiene permiso para corregir «{FIELD_LABELS[field]}» ({FIELD_CLAIMS[field]})", 403)
        if field in ("title", "article_section", "body"):
            if not isinstance(value, str) or not value.strip():
                raise ReviewError(f"«{FIELD_LABELS[field]}» no puede quedar vacío")
            value = value.strip()
        elif field == "author_raw":
            if value is not None and not isinstance(value, str):
                raise ReviewError("Autor inválido")
            value = (value or "").strip() or None
        elif field == "media_name":
            if not await db["media_sources"].find_one({"name": value, "status": "active"}):
                raise ReviewError("El medio debe existir y estar activo en el catálogo de medios")
        elif field == "content_type":
            if value not in CONTENT_TYPES:
                raise ReviewError("Tipo de contenido inválido")
        elif field == "source_destaque":
            if value not in DESTAQUES:
                raise ReviewError("Destaque inválido")
        elif field in ("source_page_number", "source_page_to"):
            if value is None and field == "source_page_to":
                pass
            elif not isinstance(value, int) or not 1 <= value <= 999:
                raise ReviewError(f"«{FIELD_LABELS[field]}» debe ser un número de página válido")
        elif field == "publication_date":
            try:
                value = date.fromisoformat(str(value)).isoformat()
            except ValueError:
                raise ReviewError("Fecha de publicación inválida (AAAA-MM-DD)")
        elif field == "provisional_topics":
            from app.services.rules import get_effective
            rule_set, _ = await get_effective()
            codes = {t["code"] for t in (rule_set or {}).get("topics", []) if t.get("active")}
            if not isinstance(value, list) or any(c not in codes for c in value):
                raise ReviewError("Los temas deben ser códigos activos del catálogo")
            value = sorted(set(value))
        elif field == "canonical_news_id":
            if value is not None:
                if value == news["id"] or not await db["news"].find_one({"id": value}):
                    raise ReviewError("La noticia original indicada no existe")
        elif field == "related_news_ids":
            if not isinstance(value, list):
                raise ReviewError("Las relacionadas deben ser una lista")
            value = sorted(set(value) - {news["id"]})
            if value and await db["news"].count_documents({"id": {"$in": value}}) != len(value):
                raise ReviewError("Alguna noticia relacionada no existe")
        before = news.get(field)
        if before != value:
            out.append({"field": field, "label": FIELD_LABELS[field], "before": before, "after": value})
    if not out:
        raise ReviewError("Los valores propuestos son iguales a los actuales")
    return out


# ---------------- Acciones ----------------
async def propose(news_id: str, changes: dict, justification: str, user: dict, claims: list[str]) -> dict:
    n = await _require_lock(news_id, user)
    justification = (justification or "").strip()
    if len(justification) < 10:
        raise ReviewError("La justificación es obligatoria (mínimo 10 caracteres)")
    diff = await _validate_changes(n, changes, claims)
    review = {
        "id": f"rev_{uuid.uuid4()}", "news_id": news_id, "kind": "correction", "status": "proposed",
        "changes": diff, "justification": justification[:2000],
        "proposed_by": user["id"], "proposed_by_email": user["email"], "proposed_at": iso_now(),
        "decided_by": None, "decided_by_email": None, "decided_at": None, "decision_note": None,
    }
    await get_db()["news_reviews"].insert_one(dict(review))
    await get_db()["news"].update_one({"id": news_id}, {"$set": {
        "review_status": "pending_approval", "pending_review_id": review["id"],
        "review_assigned_to": None, "review_locked_until": None, "updated_at": iso_now()}})
    return review


async def _get_proposal(review_id: str) -> dict:
    r = await get_db()["news_reviews"].find_one({"id": review_id})
    if not r:
        raise ReviewError("Propuesta no encontrada", 404)
    if r["status"] != "proposed":
        raise ReviewError("La propuesta ya fue resuelta", 409)
    return r


async def approve(review_id: str, user: dict, note: str) -> tuple[dict, dict]:
    r = await _get_proposal(review_id)
    if r["proposed_by"] == user["id"]:
        raise ReviewError("Doble control: quien propone una corrección no puede aprobarla", 403)
    db = get_db()
    n = await db["news"].find_one({"id": r["news_id"]})
    now = iso_now()
    updates: dict = {}
    for ch in r["changes"]:
        if n.get(ch["field"]) != ch["before"]:
            raise ReviewError(f"«{ch['label']}» cambió desde que se propuso la corrección; vuelva a revisarla", 409)
        updates[ch["field"]] = ch["after"]
    if not n.get("original_extraction"):
        updates["original_extraction"] = {f: n.get(f) for f in FIELD_CLAIMS} | {"captured_at": now}
    fields = {ch["field"] for ch in r["changes"]}
    if "publication_date" in fields:
        updates["date_basis"] = "manual"
        updates["inferred_fields"] = [f for f in n.get("inferred_fields", []) if f != "publication_date"]
    if "provisional_topics" in fields:
        updates["topics_manual"] = True
    if "canonical_news_id" in fields:
        updates["is_duplicate"] = bool(updates["canonical_news_id"])
    updates.update({
        "review_status": "corrected", "pending_review_id": None, "is_corrected": True,
        "corrected_fields": sorted(set(n.get("corrected_fields") or []) | fields),
        "last_corrected_at": now, "updated_at": now,
    })
    await db["news"].update_one({"id": n["id"]}, {"$set": updates})
    await db["news_reviews"].update_one({"id": review_id}, {"$set": {
        "status": "approved", "decided_by": user["id"], "decided_by_email": user["email"],
        "decided_at": now, "decision_note": (note or "").strip()[:2000] or None}})
    return r, n


async def return_proposal(review_id: str, user: dict, note: str) -> dict:
    r = await _get_proposal(review_id)
    note = (note or "").strip()
    if len(note) < 5:
        raise ReviewError("Indique el motivo de la devolución")
    now = iso_now()
    await get_db()["news_reviews"].update_one({"id": review_id}, {"$set": {
        "status": "returned", "decided_by": user["id"], "decided_by_email": user["email"],
        "decided_at": now, "decision_note": note[:2000]}})
    await get_db()["news"].update_one({"id": r["news_id"]}, {"$set": {
        "review_status": "pending_review", "pending_review_id": None, "updated_at": now}})
    return r


async def close_without_changes(news_id: str, user: dict, outcome: str, justification: str) -> dict:
    """outcome: approved (la extracción es correcta) | rejected (no es una pieza periodística válida)."""
    n = await _require_lock(news_id, user)
    justification = (justification or "").strip()
    if outcome == "rejected" and len(justification) < 10:
        raise ReviewError("Rechazar una pieza exige justificación (mínimo 10 caracteres)")
    now = iso_now()
    review = {
        "id": f"rev_{uuid.uuid4()}", "news_id": news_id, "kind": "approval" if outcome == "approved" else "rejection",
        "status": outcome, "changes": [], "justification": justification[:2000] or None,
        "proposed_by": user["id"], "proposed_by_email": user["email"], "proposed_at": now,
        "decided_by": user["id"], "decided_by_email": user["email"], "decided_at": now, "decision_note": None,
        "previous_review_status": "in_review", "reasons": n.get("review_reasons", []),
    }
    await get_db()["news_reviews"].insert_one(dict(review))
    await get_db()["news"].update_one({"id": news_id}, {"$set": {
        "review_status": outcome, "review_assigned_to": None, "review_locked_until": None, "updated_at": now}})
    return review


async def history(news_id: str) -> list[dict]:
    return [r async for r in get_db()["news_reviews"].find({"news_id": news_id}, projection={"_id": 0})
            .sort("proposed_at", -1)]


async def queue(match: dict, status: str, reason: str | None, user_id: str, limit: int) -> dict:
    await _expire_locks()
    db = get_db()
    base = dict(match)
    counts_status = {s: await db["news"].count_documents({**base, "review_status": s})
                     for s in ("pending_review", "in_review", "pending_approval", "corrected", "approved", "rejected")}
    reasons = {}
    async for row in await db["news"].aggregate([
        {"$match": {**base, "review_status": {"$in": list(OPEN_STATUSES)}}},
        {"$unwind": "$review_reasons"}, {"$group": {"_id": "$review_reasons", "n": {"$sum": 1}}},
    ]):
        reasons[row["_id"]] = row["n"]
    q = {**base}
    if status == "open":
        q["review_status"] = {"$in": list(OPEN_STATUSES)}
    elif status == "mine":
        q["review_status"] = "in_review"
        q["review_assigned_to"] = user_id
    else:
        q["review_status"] = status
    if reason:
        q["review_reasons"] = reason
    items = [n async for n in db["news"].find(q).sort([("summary_date", -1), ("news_index", 1)]).limit(limit)]
    proposals = {}
    ids = [n["pending_review_id"] for n in items if n.get("pending_review_id")]
    if ids:
        async for r in db["news_reviews"].find({"id": {"$in": ids}}, projection={"_id": 0}):
            proposals[r["id"]] = r
    return {"items": items, "proposals": proposals, "counts": counts_status,
            "reasons": [{"code": k, "label": REASON_LABELS.get(k, k), "count": v} for k, v in sorted(reasons.items(), key=lambda x: -x[1])],
            "total": await db["news"].count_documents(q)}
