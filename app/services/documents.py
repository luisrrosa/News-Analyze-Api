"""Recepción de documentos (Plan V3.0 §5.1 y §6).

validar → SHA-256 → rechazar duplicado → guardar original → documents + processing_jobs + outbox_events
(en una transacción cuando el servidor la admite) → publicar el trabajo en la cola.
"""
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass

import pymupdf
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.core.config import get_settings
from app.core.db import get_db, iso_now
from app.extraction.extractor import EXTRACTOR_VERSION
from app.storage import get_storage

log = logging.getLogger("documents")

PIPELINE_VERSION = EXTRACTOR_VERSION


class DocumentRejected(Exception):
    def __init__(self, status: int, message: str, code: str, existing_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.existing_id = existing_id


@dataclass
class Actor:
    user_id: str
    user_email: str
    user_roles: list[str]
    ip_address: str | None = None
    request_id: str | None = None


def validate_pdf(data: bytes) -> int:
    """Valida por contenido (firma y estructura), no por extensión. Devuelve el número de páginas."""
    s = get_settings()
    if not data:
        raise DocumentRejected(400, "El archivo está vacío.", "EMPTY_FILE")
    if len(data) > s.documents_max_size_mb * 1024 * 1024:
        raise DocumentRejected(413, f"El archivo supera el límite de {s.documents_max_size_mb} MB.", "FILE_TOO_LARGE")
    if not data.startswith(b"%PDF-"):
        raise DocumentRejected(400, "El archivo no es un PDF válido (firma de bytes incorrecta).", "NOT_PDF")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        raise DocumentRejected(400, "El PDF está dañado o no se puede abrir.", "PDF_CORRUPT")
    try:
        if doc.needs_pass or doc.is_encrypted:
            raise DocumentRejected(400, "El PDF está cifrado o protegido con contraseña.", "PDF_ENCRYPTED")
        if doc.page_count == 0:
            raise DocumentRejected(400, "El PDF no tiene páginas.", "PDF_EMPTY")
        if doc.page_count > s.documents_max_pages:
            raise DocumentRejected(413, f"El PDF supera el límite de {s.documents_max_pages} páginas.", "TOO_MANY_PAGES")
        return doc.page_count
    finally:
        doc.close()


def safe_display_name(filename: str) -> str:
    name = re.sub(r"[^\w.\- ()áéíóúÁÉÍÓÚñÑ]", "_", filename or "documento.pdf").strip()[:200]
    return name or "documento.pdf"


def idempotency_key(document_id: str, job_type: str, attempt_id: str) -> str:
    return hashlib.sha256(f"{document_id}|{job_type}|{PIPELINE_VERSION}|{attempt_id}".encode()).hexdigest()


async def _insert_all(document: dict, job: dict, event: dict) -> None:
    db = get_db()
    client = db.client
    try:
        async with client.start_session() as session:
            async with await session.start_transaction():
                await db["documents"].insert_one(document, session=session)
                await db["processing_jobs"].insert_one(job, session=session)
                await db["outbox_events"].insert_one(event, session=session)
        return
    except OperationFailure as e:
        # Servidor sin réplicas (MongoDB local de desarrollo/pruebas): sin transacciones.
        if e.code not in (20, 263) and "replica set" not in str(e).lower():
            raise
    await db["documents"].insert_one(document)
    await db["processing_jobs"].insert_one(job)
    await db["outbox_events"].insert_one(event)


async def receive_document(data: bytes, filename: str, actor: Actor, source: str) -> dict:
    page_count = validate_pdf(data)
    sha = hashlib.sha256(data).hexdigest()
    db = get_db()

    existing = await db["documents"].find_one({"file_hash": sha}, projection={"id": 1, "status": 1})
    if existing:
        raise DocumentRejected(409, "Este PDF ya fue cargado anteriormente.", "DUPLICATE_DOCUMENT", existing["id"])

    storage_key = get_storage().put(sha, data)
    now = iso_now()
    document_id = f"doc_{uuid.uuid4()}"
    job_id = f"job_{uuid.uuid4()}"
    attempt_id = str(uuid.uuid4())

    document = {
        "id": document_id,
        "file_name": safe_display_name(filename),
        "file_hash": sha,
        "file_size": len(data),
        "page_count": page_count,
        "mime_type": "application/pdf",
        "storage_key": storage_key,
        "status": "queued",
        "summary_date": None,
        "document_type": None,
        "footer_document_number": None,
        "uploaded_by": actor.user_id,
        "uploaded_at": now,
        "ingest_source": source,
        "pipeline_version": PIPELINE_VERSION,
        "items_total": 0,
        "items_with_warnings": 0,
        "duplicates_count": 0,
        "warnings": [],
        "error_summary": None,
        "created_at": now,
        "updated_at": now,
    }
    job = {
        "id": job_id,
        "document_id": document_id,
        "job_type": "extract",
        "status": "queued",
        "attempt": 0,
        "max_attempts": get_settings().job_max_attempts,
        "idempotency_key": idempotency_key(document_id, "extract", attempt_id),
        "lease_expires_at": None,
        "celery_task_id": None,
        "progress": 0,
        "last_error": None,
        "created_at": now,
        "started_at": None,
        "finished_at": None,
    }
    event = {
        "id": f"evt_{uuid.uuid4()}",
        "event_type": "document.uploaded",
        "aggregate_id": document_id,
        "payload": {"job_id": job_id},
        "status": "PENDING",
        "attempts": 0,
        "created_at": now,
        "published_at": None,
    }
    try:
        await _insert_all(document, job, event)
    except DuplicateKeyError:
        existing = await db["documents"].find_one({"file_hash": sha}, projection={"id": 1})
        raise DocumentRejected(409, "Este PDF ya fue cargado anteriormente.", "DUPLICATE_DOCUMENT",
                               existing["id"] if existing else None)

    # Publicación inmediata; si falla, el despachador del outbox la reintenta.
    from app.worker import publish_pending_events
    try:
        await publish_pending_events(limit=5)
    except Exception:
        log.warning("publicación inmediata fallida; el despachador lo reintentará", exc_info=True)

    document.pop("_id", None)
    return document
