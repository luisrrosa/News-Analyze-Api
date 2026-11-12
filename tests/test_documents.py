"""Fase 2: recepción, procesamiento, duplicados, permisos, visor, idempotencia, recuperación y carpeta."""
import os
import shutil
from datetime import timedelta
from pathlib import Path

import pymupdf
import pytest

from app.agents.folder_ingest import run_once
from app.core.config import get_settings
from app.core.db import get_db, iso_now, utcnow
from app.services.pipeline import process_job, reconcile_expired_jobs
from tests.conftest import admin_session, user_session
from tests.test_extraction import REFERENCE_PDF

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(not REFERENCE_PDF.exists(), reason="PDF de referencia no disponible"),
]


def make_pdf(items: int = 1, tag: str = "A") -> bytes:
    """PDF sintético con el formato del resumen (cabecera de ítem + titular en negrita + cuerpo)."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((90, 25), "RESUMEN DE PRENSA DIARIA", fontname="helvetica-bold", fontsize=12)
    page.insert_text((120, 40), "Martes 6 octubre 2026", fontsize=12)
    y = 90
    for n in range(items):
        page.insert_text((36, y), f"Hoy - Nacionales - Tipo Noticia - Destaque Medio - Pág. {n + 1}", fontsize=10)
        page.insert_text((36, y + 14), f"TITULAR SINTETICO {tag}{n}.", fontname="helvetica-bold", fontsize=12)
        page.insert_text((36, y + 28), f"Cuerpo de prueba {tag} numero {n} con texto suficiente para la extraccion.", fontsize=12)
        y += 60
    page.insert_text((86, 770), "Presidencia de la Republica Dominicana - DIECOM / DMAII / IRP / 228 / 6/10/2026 / Pag. 1 de 1", fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


async def upload(session, data: bytes, name: str = "resumen.pdf"):
    return await session.client.post("/api/v1/documents/upload", headers=session.headers(),
                                      files={"file": (name, data, "application/pdf")})


@pytest.fixture
async def roles(client):
    admin = await admin_session(client)
    return {"admin": admin, **{r: await user_session(client, admin, r) for r in ("editor", "consulta", "auditor")}}


@pytest.fixture
async def reference_doc(roles):
    existing = await get_db()["documents"].find_one({"file_name": "RESUMEN PRENSA 05 10 2026.pdf"})
    if existing:
        return existing["id"]
    r = await upload(roles["editor"], REFERENCE_PDF.read_bytes(), "RESUMEN PRENSA 05 10 2026.pdf")
    assert r.status_code == 202, r.text
    return r.json()["summary"]["id"]


async def test_reference_document_processed(roles, reference_doc):
    r = await roles["consulta"].get(f"/api/v1/documents/{reference_doc}")
    assert r.status_code == 200
    doc = r.json()["document"]
    assert doc["status"] == "completed"
    assert doc["total_items"] == 156 and doc["page_count"] == 39
    assert doc["summary_date"] == "2026-10-05" and doc["footer_document_number"] == "227"
    assert doc["items_by_type"] == {"Noticia": 118, "Artículo": 33, "Editorial": 5}
    assert doc["multi_page_items_count"] == 32
    assert "storage_key" not in doc
    assert r.json()["job"]["status"] == "succeeded"


async def test_news_listing_and_compat_fields(roles, reference_doc):
    r = await roles["consulta"].get(f"/api/v1/news?document_id={reference_doc}&limit=200")
    body = r.json()
    assert body["total"] == 156
    first = min(body["items"], key=lambda n: n["news_index"])
    assert first["medio"] == "Diario Libre" and first["destaque"] == "Medio" and first["autor"] == "Adalberto de la Rosa"
    assert first["title"].startswith("LAS ALCALDÍAS") and first["publication_date"] == "2026-10-05"
    assert first["triage"]["alert_level"] == "SIN_EVALUAR"
    r = await roles["consulta"].get("/api/v1/news?medio=El Día&limit=200")
    assert r.json()["total"] >= 21


async def test_related_pair_linked_not_duplicated(roles, reference_doc):
    col = get_db()["news"]
    a = await col.find_one({"document_id": reference_doc, "news_index": 102})
    b = await col.find_one({"document_id": reference_doc, "news_index": 147})
    assert not b["is_duplicate"] and a["id"] in b["related_news_ids"]


async def test_duplicate_document_rejected(roles, reference_doc):
    r = await upload(roles["editor"], REFERENCE_PDF.read_bytes(), "copia.pdf")
    assert r.status_code == 409 and r.json()["code"] == "DUPLICATE_DOCUMENT"
    assert await get_db()["documents"].count_documents({"file_hash": {"$exists": True}, "file_name": "copia.pdf"}) == 0


async def test_upload_validation_and_permissions(roles):
    r = await upload(roles["editor"], b"esto no es un pdf", "falso.pdf")
    assert r.status_code == 400 and r.json()["code"] == "NOT_PDF"
    r = await upload(roles["consulta"], make_pdf(tag="P"), "x.pdf")
    assert r.status_code == 403
    r = await upload(roles["auditor"], make_pdf(tag="Q"), "x.pdf")
    assert r.status_code == 403


async def test_download_and_viewer_permissions(roles, reference_doc):
    for role, code in (("consulta", 403), ("auditor", 403), ("editor", 200), ("admin", 200)):
        r = await roles[role].get(f"/api/v1/documents/{reference_doc}/download")
        assert r.status_code == code, role
        if code == 200:
            assert r.content.startswith(b"%PDF-") and r.headers["cache-control"] == "no-store"
    for role in ("consulta", "auditor"):
        r = await roles[role].get(f"/api/v1/documents/{reference_doc}/pages/1/image")
        assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
        assert r.content[:4] == b"RIFF" and r.content[8:12] == b"WEBP"
    r = await roles["consulta"].get(f"/api/v1/documents/{reference_doc}/pages/2/render")
    items = r.json()["items_in_page"]
    assert any(i["news_index"] == 4 and i["continues_from_prev_page"] for i in items)
    assert (await roles["consulta"].get(f"/api/v1/documents/{reference_doc}/pages/99/image")).status_code == 400


async def test_redelivery_is_idempotent(roles, reference_doc):
    job = await get_db()["processing_jobs"].find_one({"document_id": reference_doc})
    assert await process_job(job["id"]) == "skipped"  # ya terminado: una entrega duplicada no hace nada
    # Simula una reentrega tras caída: el trabajo vuelve a la cola y se ejecuta otra vez.
    await get_db()["processing_jobs"].update_one({"id": job["id"]}, {"$set": {"status": "queued"}})
    assert await process_job(job["id"]) == "done"
    assert await get_db()["news"].count_documents({"document_id": reference_doc}) == 156


async def test_reconcile_requeues_expired_lease(roles):
    r = await upload(roles["editor"], make_pdf(2, tag="R"), "reconcile.pdf")
    doc_id = r.json()["summary"]["id"]
    jobs = get_db()["processing_jobs"]
    await jobs.update_one({"document_id": doc_id}, {"$set": {
        "status": "running", "attempt": 1, "lease_expires_at": utcnow() - timedelta(seconds=5)}})
    assert await reconcile_expired_jobs() >= 1
    job = await jobs.find_one({"document_id": doc_id})
    assert job["status"] == "retry"
    assert await get_db()["outbox_events"].count_documents({"payload.job_id": job["id"], "status": "PENDING"}) == 1


async def test_document_without_items_fails_explicitly(roles):
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 100), "Documento sin cabeceras de noticias.")
    r = await upload(roles["editor"], doc.tobytes(), "vacio.pdf")
    doc.close()
    summary = r.json()["summary"]
    assert summary["status"] == "failed" and "ninguna noticia" in summary["error_summary"]


async def test_folder_ingest(roles, reference_doc):
    inbox = Path(get_settings().ingest_folder)
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "nuevo.pdf").write_bytes(make_pdf(3, tag="F"))
    shutil.copy(REFERENCE_PDF, inbox / "repetido.pdf")
    (inbox / "notas.txt").write_text("ignorar")
    result = await run_once(wait_seconds=0.2)
    assert [p["file"] for p in result["processed"]] == ["nuevo.pdf"]
    assert result["rejected"] == [{"file": "repetido.pdf", "reason": "DUPLICATE_DOCUMENT"}]
    assert not (inbox / "nuevo.pdf").exists() and not (inbox / "repetido.pdf").exists()
    assert any(p.name.endswith("_nuevo.pdf") for p in (inbox / "procesados").iterdir())
    motivo = [p for p in (inbox / "rechazados").iterdir() if p.name.endswith(".motivo.txt")]
    assert motivo and "DUPLICATE_DOCUMENT" in motivo[0].read_text(encoding="utf-8")
    doc = await get_db()["documents"].find_one({"id": result["processed"][0]["document_id"]})
    assert doc["ingest_source"] == "folder" and doc["status"] in ("completed", "completed_with_warnings")


async def test_folder_ingest_ignores_symlinks(roles):
    inbox = Path(get_settings().ingest_folder)
    link = inbox / "enlace.pdf"
    try:
        os.symlink(REFERENCE_PDF, link)
    except (OSError, NotImplementedError):
        pytest.skip("El sistema no permite crear enlaces simbólicos sin privilegios")
    result = await run_once(wait_seconds=0.2)
    assert "enlace.pdf" in result["symlinks_ignored"] and link.is_symlink()
    link.unlink()
