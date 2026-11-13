"""Procesamiento de un documento (worker). Plan V3.0 §5 y §6.

- Idempotente: las noticias se insertan con clave única (document_id, news_index); una entrega doble no duplica.
- Un ítem defectuoso no detiene el documento: el estado final es completed_with_warnings.
- El estado del documento se deriva del trabajo; nunca se escribe a mano desde la interfaz.
"""
import logging
import uuid
from datetime import timedelta

from pymongo import UpdateOne

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.db import get_db, iso_now, utcnow
from app.extraction.extractor import ExtractedItem, extract
from app.extraction.simhash import DUPLICATE_MAX, RELATED_MAX, SIMHASH_VERSION, from_hex, hamming, simhash64, to_hex
from app.storage import get_storage

log = logging.getLogger("pipeline")

# Señales de extracción que exigen revisión humana (Plan V3.0 §11.6, capa determinista).
REVIEW_FLAGS = {"title_not_found", "media_unresolved", "text_quality_failed"}
WARNING_FLAGS = REVIEW_FLAGS | {"multiple_urls"}
RETRY_BACKOFF_SECONDS = [30, 120, 480]


class PermanentError(Exception):
    """Error que no se resuelve reintentando (p. ej. el original no existe)."""


async def _recent_hashes(exclude_document: str) -> list[tuple[str, int]]:
    since = (utcnow() - timedelta(days=get_settings().dedupe_window_days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    cursor = get_db()["news"].find(
        {"created_at": {"$gte": since}, "is_duplicate": False, "document_id": {"$ne": exclude_document}},
        projection={"id": 1, "simhash": 1},
    )
    return [(d["id"], from_hex(d["simhash"])) async for d in cursor if d.get("simhash")]


def _news_doc(item: ExtractedItem, document: dict, news_id: str, hash_hex: str, dedupe: dict, now: str) -> dict:
    flags = list(item.flags)
    reasons = [f for f in flags if f in REVIEW_FLAGS]
    if dedupe["is_duplicate"]:
        reasons.append("duplicate_link_verification")
    return {
        "id": news_id,
        "document_id": document["id"],
        "file_hash": document["file_hash"],
        "news_index": item.news_index,
        "content_type": item.content_type,
        "document_block": item.document_block,
        "article_section": item.article_section,
        "media_raw": item.media_raw,
        "media_name": item.media_name,
        "source_destaque": item.source_destaque,
        "source_page_number": item.source_page_number,
        "source_page_to": item.source_page_to,
        "pdf_page_indices": item.pdf_page_indices,
        "author_raw": item.author_raw,
        "title": item.title,
        "header_raw": item.header_raw,
        "summary_date": document.get("summary_date"),
        "publication_date": item.publication_date,
        "date_basis": item.date_basis,
        "inferred_fields": ["publication_date"] if item.date_basis == "from_summary" else [],
        "url": item.url,
        "url_domain": item.url_domain,
        "text_original": item.text_original,
        "body": item.body,
        "word_count": item.word_count,
        "extraction_quality": item.quality["extraction_quality"],
        "quality": item.quality,
        "text_quality_failed": item.quality["text_quality_failed"],
        "segmentation_confidence": item.segmentation_confidence,
        "extraction_flags": flags,
        "simhash": hash_hex,
        "simhash_version": SIMHASH_VERSION,
        **dedupe,
        "review_status": "pending_review" if reasons else "not_required",
        "review_reasons": reasons,
        "pipeline_version": document["pipeline_version"],
        "created_at": now,
        "updated_at": now,
    }


async def process_job(job_id: str) -> str:
    """Ejecuta un trabajo de extracción. Devuelve 'done', 'retry', 'dead' o 'skipped'."""
    db = get_db()
    s = get_settings()
    now_dt = utcnow()
    # Concesión atómica: solo un worker toma el trabajo (evita doble ejecución por entrega duplicada).
    job = await db["processing_jobs"].find_one_and_update(
        {"id": job_id, "status": {"$in": ["queued", "retry"]}},
        {"$set": {"status": "running", "started_at": iso_now(), "lease_expires_at": now_dt + timedelta(seconds=s.job_lease_seconds)},
         "$inc": {"attempt": 1}},
        return_document=True,
    )
    if not job:
        return "skipped"
    document = await db["documents"].find_one({"id": job["document_id"]})
    if not document:
        await db["processing_jobs"].update_one({"id": job_id}, {"$set": {"status": "dead", "last_error": "Documento inexistente"}})
        return "dead"
    await db["documents"].update_one({"id": document["id"]}, {"$set": {"status": "processing", "updated_at": iso_now()}})

    try:
        data = get_storage().get(document["storage_key"])
        if data is None:
            raise PermanentError("El archivo original no está en el almacenamiento")
        from app.services.catalogs import media_alias_map
        result = extract(data, await media_alias_map() or None)
        if not result.items:
            raise PermanentError("No se identificó ninguna noticia en el documento")

        # Toda noticia necesita fecha del resumen: si el documento no la trae, se usa la fecha de carga (marcada como inferida).
        summary_date = result.summary_date or str(document["uploaded_at"])[:10]
        summary_date_inferred = result.summary_date is None

        recent = await _recent_hashes(document["id"])
        seen: list[tuple[str, int]] = []
        operations, duplicates, item_errors, flagged = [], 0, [], 0
        now = iso_now()

        for item in result.items:
            try:
                h = simhash64(item.text_original)
                best_id, best_d, related = None, 65, []
                for other_id, oh in recent + seen:
                    d = hamming(h, oh)
                    if d <= RELATED_MAX:
                        related.append(other_id)
                    if d < best_d:
                        best_id, best_d = other_id, d
                is_dup = best_d <= DUPLICATE_MAX
                dedupe = {
                    "is_duplicate": is_dup,
                    "canonical_news_id": best_id if is_dup else None,
                    "related_news_ids": [] if is_dup else related[:10],
                    "duplicate_distance": best_d if best_d <= RELATED_MAX else None,
                    "duplicate_similarity": round(1 - best_d / 64, 4) if best_d <= RELATED_MAX else None,
                }
                news_id = f"news_{uuid.uuid4()}"
                doc = _news_doc(item, {**document, "summary_date": summary_date}, news_id, to_hex(h), dedupe, now)
                if summary_date_inferred:
                    doc["inferred_fields"] = sorted(set(doc["inferred_fields"]) | {"summary_date"})
                if doc["publication_date"] is None:
                    doc["publication_date"], doc["date_basis"] = summary_date, "from_summary"
                    doc["inferred_fields"] = sorted(set(doc["inferred_fields"]) | {"publication_date"})
                operations.append(UpdateOne(
                    {"document_id": document["id"], "news_index": item.news_index},
                    {"$setOnInsert": doc},
                    upsert=True,
                ))
                if not is_dup:
                    seen.append((news_id, h))
                duplicates += int(is_dup)
                flagged += int(any(f in WARNING_FLAGS for f in item.flags))
            except Exception as e:  # un ítem no detiene el documento
                log.exception("ítem %s del documento %s", item.news_index, document["id"])
                item_errors.append(f"Ítem {item.news_index}: {e}")

        if operations:
            await db["news"].bulk_write(operations, ordered=False)

        warnings = result.warnings + item_errors
        status = "completed_with_warnings" if (warnings or flagged) else "completed"
        await db["documents"].update_one({"id": document["id"]}, {"$set": {
            "status": status,
            "summary_date": summary_date,
            "summary_date_inferred": summary_date_inferred,
            "document_type": result.document_type,
            "footer_document_number": result.footer_document_number,
            "page_count": result.page_count,
            "page_quality": result.page_quality,
            "items_total": len(result.items) - len(item_errors),
            "items_with_warnings": flagged,
            "duplicates_count": duplicates,
            "warnings": warnings[:200],
            "error_summary": None,
            "updated_at": iso_now(),
        }})
        await db["processing_jobs"].update_one({"id": job_id}, {"$set": {
            "status": "succeeded", "progress": 100, "finished_at": iso_now(), "lease_expires_at": None, "last_error": None}})
        await write_audit(
            user_id=document["uploaded_by"], user_email="", user_roles=[], action="DOC_PARSE_COMPLETE",
            entity_type="DOCUMENT", entity_id=document["id"], status="SUCCESS",
            details={"file_name": document["file_name"], "sha256": document["file_hash"], "status": status,
                     "items": len(result.items), "duplicates": duplicates, "warnings": len(warnings),
                     "pipeline_version": document["pipeline_version"], "origin": document["ingest_source"]},
        )
        return "done"

    except PermanentError as e:
        return await _fail(job, document, str(e), permanent=True)
    except Exception as e:
        log.exception("fallo procesando %s", document["id"])
        return await _fail(job, document, f"{type(e).__name__}: {e}", permanent=False)


async def _fail(job: dict, document: dict, message: str, permanent: bool) -> str:
    db = get_db()
    attempts = job["attempt"]
    dead = permanent or attempts >= job["max_attempts"]
    await db["processing_jobs"].update_one({"id": job["id"]}, {"$set": {
        "status": "dead" if dead else "retry", "last_error": message[:1000], "lease_expires_at": None,
        "finished_at": iso_now() if dead else None}})
    if dead:
        await db["documents"].update_one({"id": document["id"]}, {"$set": {
            "status": "failed", "error_summary": message[:500], "updated_at": iso_now()}})
        await write_audit(user_id=document["uploaded_by"], user_email="", user_roles=[], action="DOC_PARSE_FAILED",
                          entity_type="DOCUMENT", entity_id=document["id"], status="FAILURE", error_message=message[:300])
        return "dead"
    return "retry"


async def reconcile_expired_jobs() -> int:
    """Devuelve a la cola los trabajos cuya concesión venció (worker caído). Plan V3.0 §6.6."""
    db = get_db()
    count = 0
    async for job in db["processing_jobs"].find({"status": "running", "lease_expires_at": {"$lt": utcnow()}}):
        if job["attempt"] >= job["max_attempts"]:
            doc = await db["documents"].find_one({"id": job["document_id"]}) or {"id": job["document_id"], "uploaded_by": "sistema"}
            await _fail(job, doc, "Concesión vencida y sin intentos restantes", permanent=True)
            continue
        res = await db["processing_jobs"].update_one(
            {"id": job["id"], "status": "running"},
            {"$set": {"status": "retry", "lease_expires_at": None, "last_error": "Concesión vencida: worker caído o detenido"}})
        if res.modified_count:
            await db["outbox_events"].insert_one({
                "id": f"evt_{uuid.uuid4()}", "event_type": "job.retry", "aggregate_id": job["document_id"],
                "payload": {"job_id": job["id"]}, "status": "PENDING", "attempts": 0,
                "created_at": iso_now(), "published_at": None})
            count += 1
    return count
