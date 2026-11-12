import io

import pymupdf
from fastapi import APIRouter, Depends, File, Path, Query, Request, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFont

from app.api.compat import document_out, news_out
from app.api.deps import ApiError, AuthContext, require
from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.db import get_db, iso_now
from app.services.documents import Actor, DocumentRejected, receive_document
from app.storage import get_storage

router = APIRouter(prefix="/documents", tags=["documents"])


async def _stats(document_id: str) -> tuple[dict, dict, int]:
    by_type, by_media, multi = {}, {}, 0
    async for row in await get_db()["news"].aggregate([
        {"$match": {"document_id": document_id}},
        {"$group": {"_id": {"t": "$content_type", "m": "$media_name"}, "n": {"$sum": 1},
                    "multi": {"$sum": {"$cond": [{"$gt": [{"$size": "$pdf_page_indices"}, 1]}, 1, 0]}}}},
    ]):
        by_type[row["_id"]["t"]] = by_type.get(row["_id"]["t"], 0) + row["n"]
        media = row["_id"]["m"] or "Sin resolver"
        by_media[media] = by_media.get(media, 0) + row["n"]
        multi += row["multi"]
    return by_type, by_media, multi


async def _get_document(document_id: str) -> dict:
    doc = await get_db()["documents"].find_one({"id": document_id})
    if not doc:
        raise ApiError(404, "Documento no encontrado")
    return doc


@router.get("")
async def list_documents(
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), status: str | None = None,
    ctx: AuthContext = Depends(require("documents:read")),
):
    q = {"status": status} if status else {}
    col = get_db()["documents"]
    total = await col.count_documents(q)
    docs = []
    async for d in col.find(q).sort("created_at", -1).skip(offset).limit(limit):
        docs.append(document_out(d, *await _stats(d["id"])))
    return {"documents": docs, "total": total}


@router.post("/upload", status_code=202)
async def upload(request: Request, file: UploadFile = File(...), ctx: AuthContext = Depends(require("documents:upload"))):
    max_bytes = get_settings().documents_max_size_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)
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
        "message": f"Documento {out['filename']} recibido. Estado: {out['status']}.",
        "summary": out,
        "items_extracted": out["total_items"],
    }


@router.post("/ingest-folder")
async def scan_folder(ctx: AuthContext = Depends(require("documents:upload"))):
    """Escaneo inmediato de la carpeta de ingesta (el agente también la vigila de forma periódica)."""
    from app.agents.folder_ingest import run_once
    result = await run_once(wait_seconds=min(get_settings().ingest_stable_seconds, 5))
    n_ok, n_bad = len(result["processed"]), len(result["rejected"])
    await write_audit(**ctx.actor(), action="FOLDER_INGEST_SCAN", entity_type="FOLDER", entity_id="inbox",
                      status="SUCCESS", details={"processed": n_ok, "rejected": n_bad})
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
    data = get_storage().get(doc["storage_key"])
    if data is None:
        raise ApiError(404, "El archivo original no está disponible")
    await write_audit(**ctx.actor(), action="PDF_DOWNLOAD_AUTHORIZED", entity_type="DOCUMENT", entity_id=document_id,
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
        i["continues_from_prev_page"] = i["pdf_pages"][0] < page
        i["continues_to_next_page"] = i["pdf_pages"][-1] > page
    return {"page": page, "page_count": doc["page_count"], "document_id": document_id, "items_in_page": items,
            "watermark": "USO EXCLUSIVO INSTITUCIONAL"}


@router.get("/{document_id}/pages/{page}/image")
async def page_image(document_id: str, page: int = Path(ge=1, le=1000), ctx: AuthContext = Depends(require("documents:read"))):
    """Visor sin descarga (decisión D-02): la página se entrega como imagen WebP con marca de agua del usuario."""
    doc = await _get_document(document_id)
    if page > (doc.get("page_count") or 0):
        raise ApiError(400, "Número de página inválido")
    data = get_storage().get(doc["storage_key"])
    if data is None:
        raise ApiError(404, "El archivo original no está disponible")
    pdf = pymupdf.open(stream=data, filetype="pdf")
    try:
        pix = pdf[page - 1].get_pixmap(dpi=110)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        pdf.close()

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    mark = f"USO INSTITUCIONAL · {ctx.user['email']} · {iso_now()[:16].replace('T', ' ')} UTC"
    for y in range(80, img.height, 260):
        draw.text((40, y), mark, fill=(180, 30, 30, 70), font=font)
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=80)

    await write_audit(**ctx.actor(), action="DOC_PAGES_RENDER_ACCESSED", entity_type="DOCUMENT", entity_id=document_id,
                      status="SUCCESS", details={"page": page})
    return Response(buf.getvalue(), media_type="image/webp", headers={"Cache-Control": "private, no-store"})
