import asyncio
import logging
import io
import threading
from collections import OrderedDict

import pymupdf
from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import Response
from starlette.datastructures import UploadFile as StarletteUploadFile
from PIL import Image, ImageDraw, ImageFont

from app.api.compat import document_out, news_out
from app.api.dates import summary_date_filter
from app.api.deps import ApiError, AuthContext, require, require_any
from app.core.audit import write_audit, write_audit_required
from app.core.config import get_settings
from app.core.db import get_db, iso_now, utcnow
from app.services.documents import Actor, DocumentRejected, receive_document
from app.storage import read_original

log = logging.getLogger("documents")

router = APIRouter(prefix="/documents", tags=["documents"])


async def _stats_many(document_ids: list[str]) -> dict[str, tuple[dict, dict, int]]:
    """Distribución por tipo y medio de varios documentos en una sola agregación."""
    out: dict[str, tuple[dict, dict, int]] = {d: ({}, {}, 0) for d in document_ids}
    if not document_ids:
        return out
    multi: dict[str, int] = {d: 0 for d in document_ids}
    async for row in await get_db()["news"].aggregate([
        {"$match": {"document_id": {"$in": document_ids}}},
        {"$group": {"_id": {"d": "$document_id", "t": "$content_type", "m": "$media_name"}, "n": {"$sum": 1},
                    "multi": {"$sum": {"$cond": [{"$gt": [{"$size": "$pdf_page_indices"}, 1]}, 1, 0]}}}},
    ]):
        d = row["_id"]["d"]
        by_type, by_media, _ = out[d]
        by_type[row["_id"]["t"]] = by_type.get(row["_id"]["t"], 0) + row["n"]
        media = row["_id"]["m"] or "Sin resolver"
        by_media[media] = by_media.get(media, 0) + row["n"]
        multi[d] += row["multi"]
    return {d: (out[d][0], out[d][1], multi[d]) for d in document_ids}


async def _stats(document_id: str) -> tuple[dict, dict, int]:
    return (await _stats_many([document_id]))[document_id]


async def _get_document(document_id: str) -> dict:
    doc = await get_db()["documents"].find_one({"id": document_id})
    if not doc:
        raise ApiError(404, "Documento no encontrado")
    return doc


@router.get("")
async def list_documents(
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), status: str | None = None,
    date_from: str | None = None, date_to: str | None = None,
    ctx: AuthContext = Depends(require("documents:read")),
):
    q: dict = {"status": status} if status else {}
    f = summary_date_filter(date_from, date_to)
    if f:
        # Los documentos aún en cola no tienen fecha del resumen: se muestran siempre hasta que terminen.
        q["$or"] = [f, {"status": {"$in": ["queued", "processing"]}}]
    col = get_db()["documents"]
    total = await col.count_documents(q)
    raw = [d async for d in col.find(q, projection={"page_quality": 0, "warnings": 0})
           .sort([("summary_date", -1), ("created_at", -1)]).skip(offset).limit(limit)]
    stats = await _stats_many([d["id"] for d in raw])
    return {"documents": [document_out(d, *stats[d["id"]]) for d in raw], "total": total}


@router.get("/status")
async def processing_status(ctx: AuthContext = Depends(require_any("documents:read", "documents:upload", "dashboard:read"))):
    """Consulta ligera para el sondeo de la interfaz: cuántos documentos siguen en cola o procesándose y la
    marca de la última actualización (la interfaz recarga todo solo cuando cambia)."""
    col = get_db()["documents"]
    pending = await col.count_documents({"status": {"$in": ["queued", "processing"]}})
    last = await col.find_one({}, projection={"_id": 0, "updated_at": 1}, sort=[("updated_at", -1)])
    return {"pending": pending, "last_update": (last or {}).get("updated_at")}


@router.post("/upload", status_code=202)
async def upload(request: Request, ctx: AuthContext = Depends(require("documents:upload"))):
    # Sin parámetro File(): así el cuerpo NO se lee antes de validar la sesión y el permiso.
    # El tamaño total ya lo limita BodyLimitMiddleware; aquí, un solo archivo y su tamaño máximo.
    max_bytes = get_settings().documents_max_size_mb * 1024 * 1024
    try:
        form = await request.form(max_files=1, max_fields=4, max_part_size=64 * 1024)
    except Exception:
        raise ApiError(400, "Formulario de carga inválido", "INVALID_FORM")
    file = form.get("file")
    if not isinstance(file, StarletteUploadFile):
        raise ApiError(400, "Falta el archivo PDF (campo file)", "NO_FILE")
    data = await file.read(max_bytes + 1)
    await form.close()
    try:
        doc = await receive_document(data, file.filename or "documento.pdf",
                                     Actor(**{k: v for k, v in ctx.actor().items()}), source="manual")
    except DocumentRejected as e:
        await write_audit(**ctx.actor(), action="DOC_UPLOAD_REJECTED", entity_type="DOCUMENT",
                          entity_id=e.existing_id or "-", status="DENIED", error_message=e.message,
                          details={"file_name": file.filename, "code": e.code})
        message = f"{e.message} (documento {e.existing_id})" if e.existing_id else e.message
        raise ApiError(e.status, message, e.code)
    await write_audit(**ctx.actor(), action="DOC_UPLOADED", entity_type="DOCUMENT", entity_id=doc["id"],
                      status="SUCCESS", details={"file_name": doc["file_name"], "sha256": doc["file_hash"],
                                                 "pages": doc["page_count"], "origin": "manual"})
    fresh = await _get_document(doc["id"])  # en modo síncrono ya viene procesado
    out = document_out(fresh, *await _stats(doc["id"]))
    return {
        "success": True,
        "message": f"Documento {out['file_name']} recibido. Estado: {out['status']}.",
        "summary": out,
        "items_extracted": out.get("items_total", 0),
    }


@router.post("/ingest-folder")
async def scan_folder(ctx: AuthContext = Depends(require("documents:ingest_scan"))):
    """Revisión inmediata del origen de ingesta: el SFTP si está configurado (AKS), si no la carpeta local.
    El worker también la hace de forma periódica."""
    from app import sftp
    if sftp.configured():
        from app.agents.sftp_ingest import run_once as sftp_once
        try:
            result = await sftp_once()
        except Exception:
            log.exception("revisión SFTP manual")
            raise ApiError(502, "No se pudo conectar con el servidor SFTP de ingesta", "SFTP_UNAVAILABLE")
        origin = "sftp"
    else:
        from app.agents.folder_ingest import run_once
        result = await run_once(wait_seconds=min(get_settings().ingest_stable_seconds, 5))
        origin = "folder"
    n_ok, n_bad = len(result["processed"]), len(result["rejected"])
    await write_audit(**ctx.actor(), action="FOLDER_INGEST_SCAN", entity_type="FOLDER", entity_id=origin,
                      status="SUCCESS", details={"processed": n_ok, "rejected": n_bad, "origin": origin,
                                                 "waiting": result.get("waiting", 0)})
    message = (f"Escaneo completado: {n_ok} documento(s) recibido(s), {n_bad} rechazado(s)."
               if n_ok or n_bad else "Escaneo completado: no hay PDF nuevos y estables en la carpeta de ingesta.")
    return {"success": True, "message": message, "processed_count": n_ok, "rejected": result["rejected"],
            "processed_files": [p["file"] for p in result["processed"]]}


@router.get("/{document_id}")
async def get_document(document_id: str, ctx: AuthContext = Depends(require("documents:read"))):
    doc = await _get_document(document_id)
    job = await get_db()["processing_jobs"].find_one({"document_id": document_id}, sort=[("created_at", -1)],
                                                     projection={"_id": 0, "status": 1, "attempt": 1, "last_error": 1, "progress": 1})
    return {"document": document_out(doc, *await _stats(document_id)), "job": job}


@router.get("/{document_id}/download")
async def download(document_id: str, ctx: AuthContext = Depends(require("documents:download"))):
    doc = await _get_document(document_id)
    data = await asyncio.to_thread(read_original, doc["storage_key"], doc["file_hash"])
    if data is None:
        raise ApiError(404, "El archivo original no está disponible")
    await write_audit_required(**ctx.actor(), action="PDF_DOWNLOAD_AUTHORIZED", entity_type="DOCUMENT", entity_id=document_id,
                      status="SUCCESS", details={"file_name": doc["file_name"], "sha256": doc["file_hash"]})
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c != '"' else "_" for c in doc["file_name"])
    from urllib.parse import quote
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(doc['file_name'])}",
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/{document_id}/pages/{page}/render")
async def page_items(document_id: str, page: int = Path(ge=1, le=1000), ctx: AuthContext = Depends(require("documents:read"))):
    """Noticias presentes en una página del PDF (visor de texto)."""
    doc = await _get_document(document_id)
    if page > (doc.get("page_count") or 0):
        raise ApiError(400, "Número de página inválido")
    items = [news_out(n) async for n in get_db()["news"].find({"document_id": document_id, "pdf_page_indices": page}).sort("news_index", 1)]
    for i in items:
        i["continues_from_prev_page"] = i["pdf_page_indices"][0] < page
        i["continues_to_next_page"] = i["pdf_page_indices"][-1] > page
    return {"page": page, "page_count": doc["page_count"], "document_id": document_id, "items_in_page": items,
            "watermark": "USO EXCLUSIVO INSTITUCIONAL"}


@router.get("/{document_id}/pages/{page}/image")
async def page_image(document_id: str, page: int = Path(ge=1, le=1000), news_id: str | None = None,
                     ctx: AuthContext = Depends(require("documents:read"))):
    """Visor sin descarga (decisión D-02): la página se entrega como imagen WebP con marca de agua del usuario.
    Con news_id, se resalta la zona de la página que ocupa esa noticia."""
    doc = await _get_document(document_id)
    if page > (doc.get("page_count") or 0):
        raise ApiError(400, "Número de página inválido")
    data = await asyncio.to_thread(read_original, doc["storage_key"], doc["file_hash"])
    if data is None:
        raise ApiError(404, "El original no está disponible: los PDF subidos desde una PC no se conservan; "
                            "los del file server se leen de su ubicación (puede haberse movido o modificado).")

    highlight = None
    if news_id:
        target = await get_db()["news"].find_one({"id": news_id, "document_id": document_id},
                                                 projection={"news_index": 1, "pdf_page_indices": 1})
        if target and page in target.get("pdf_page_indices", []):
            pages = target["pdf_page_indices"]
            if pages[0] == page:
                # Posición entre las noticias que comienzan en esta página
                order = await get_db()["news"].count_documents({
                    "document_id": document_id, "pdf_page_indices.0": page, "news_index": {"$lt": target["news_index"]}})
                highlight = (order, False, pages[-1] > page)
            else:
                highlight = (None, True, pages[-1] > page)

    mark = f"USO INSTITUCIONAL · {ctx.user['email']} · {iso_now()[:16].replace('T', ' ')} UTC"
    cache_key = (doc["file_hash"], page, highlight)
    async with _RENDER_SLOTS:
        webp = await asyncio.to_thread(_render_page, data, page, highlight, mark, cache_key)

    # Auditoría del visor: un registro por usuario, documento y hora (no uno por página vista).
    if await _first_view_this_hour(ctx.user["id"], document_id):
        await write_audit(**ctx.actor(), action="DOC_PAGES_RENDER_ACCESSED", entity_type="DOCUMENT", entity_id=document_id,
                          status="SUCCESS", details={"page": page})
    return Response(webp, media_type="image/webp", headers={"Cache-Control": "private, no-store"})


# --- Visor: render en un hilo (no bloquea el event loop), como máximo 2 a la vez por proceso.
_RENDER_SLOTS = asyncio.Semaphore(2)
_MAX_RENDER_PIXELS = 4_000_000          # ~A3 a 110 ppp; páginas mayores se reducen
_RENDER_DPI = 110
try:
    _FONT = ImageFont.truetype("DejaVuSans.ttf", 22)
except OSError:
    try:
        _FONT = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        _FONT = ImageFont.load_default()
# Caché de la página ya dibujada (sin la marca de agua, que es por usuario y hora).
_BASE_CACHE: "OrderedDict[tuple, Image.Image]" = OrderedDict()
_BASE_CACHE_MAX = 24
_cache_lock = threading.Lock()


def _render_base(data: bytes, page: int, highlight) -> Image.Image:
    pdf = pymupdf.open(stream=data, filetype="pdf")
    try:
        pg = pdf[page - 1]
        if highlight:
            from app.extraction.extractor import item_band
            band = item_band(pg, *highlight)
            if band:
                pg.draw_rect(band, color=(0.85, 0.55, 0.0), fill=(1.0, 0.85, 0.2), fill_opacity=0.18, width=1.6)
        w_pt, h_pt = pg.rect.width, pg.rect.height
        zoom = _RENDER_DPI / 72
        if w_pt * h_pt * zoom * zoom > _MAX_RENDER_PIXELS:   # página gigante: se reduce (evita agotar memoria)
            zoom = (_MAX_RENDER_PIXELS / (w_pt * h_pt)) ** 0.5
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        pdf.close()


def _render_page(data: bytes, page: int, highlight, mark: str, cache_key: tuple) -> bytes:
    with _cache_lock:
        base = _BASE_CACHE.get(cache_key)
        if base is not None:
            _BASE_CACHE.move_to_end(cache_key)
    if base is None:
        base = _render_base(data, page, highlight)
        with _cache_lock:
            _BASE_CACHE[cache_key] = base
            while len(_BASE_CACHE) > _BASE_CACHE_MAX:
                _BASE_CACHE.popitem(last=False)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for y in range(80, base.height, 260):
        draw.text((40, y), mark, fill=(180, 30, 30, 70), font=_FONT)
    img = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=80, method=2)  # method=2: ~4 veces más rápido que el predeterminado, mismo tamaño
    return buf.getvalue()


async def _first_view_this_hour(user_id: str, document_id: str) -> bool:
    key = f"{user_id}:{document_id}:{iso_now()[:13]}"
    res = await get_db()["viewer_access"].update_one({"key": key}, {"$setOnInsert": {"key": key, "created_at": utcnow()}}, upsert=True)
    return res.upserted_id is not None


@router.post("/{document_id}/reprocess", status_code=202)
async def reprocess(document_id: str, ctx: AuthContext = Depends(require("system:reprocess"))):
    """Vuelve a encolar la extracción de un documento FALLIDO (los procesados no se tocan: tienen noticias y
    correcciones de revisión). Requiere que el original siga disponible en su origen."""
    import uuid
    from app.services.documents import PIPELINE_VERSION, idempotency_key
    doc = await _get_document(document_id)
    if doc.get("status") != "failed":
        raise ApiError(409, "Solo se reprocesan documentos con estado fallido", "NOT_FAILED")
    if await asyncio.to_thread(read_original, doc["storage_key"], doc["file_hash"]) is None:
        raise ApiError(409, "El original ya no está disponible; vuelva a subir el PDF", "ORIGINAL_UNAVAILABLE")
    now = iso_now()
    job_id = f"job_{uuid.uuid4()}"
    db = get_db()
    await db["processing_jobs"].insert_one({
        "id": job_id, "document_id": document_id, "job_type": "extract", "status": "queued", "attempt": 0,
        "max_attempts": get_settings().job_max_attempts,
        "idempotency_key": idempotency_key(document_id, "extract", str(uuid.uuid4())), "lease_expires_at": None,
        "celery_task_id": None, "progress": 0, "last_error": None, "created_at": now, "started_at": None, "finished_at": None})
    await db["outbox_events"].insert_one({
        "id": f"evt_{uuid.uuid4()}", "event_type": "document.reprocess", "aggregate_id": document_id,
        "payload": {"job_id": job_id}, "status": "PENDING", "attempts": 0, "created_at": now, "published_at": None})
    await db["documents"].update_one({"id": document_id, "status": "failed"}, {"$set": {
        "status": "queued", "error_summary": None, "pipeline_version": PIPELINE_VERSION, "updated_at": now}})
    await write_audit(**ctx.actor(), action="DOC_REPROCESS_REQUESTED", entity_type="DOCUMENT", entity_id=document_id,
                      status="SUCCESS", details={"file_name": doc["file_name"], "previous_error": doc.get("error_summary")})
    from app.worker import publish_pending_events
    try:
        await publish_pending_events(limit=5)
    except Exception:
        log.warning("publicación inmediata fallida; el despachador lo reintentará", exc_info=True)
    return {"success": True, "message": "Documento enviado a reprocesar."}
