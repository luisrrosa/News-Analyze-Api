"""Despliegue AKS: PDF en memoria (Redis), lectura del original en el SFTP de origen, sondas y variables sin definir.

- Memoria: necesita un Redis (TEST_REDIS_URL, por defecto redis://localhost:6379/15); si no hay, se omite.
- SFTP: necesita un servidor de prueba (SFTP_TEST_HOST, SFTP_TEST_PORT, SFTP_TEST_USER, SFTP_TEST_PASSWORD,
  SFTP_TEST_PATH y SFTP_TEST_LOCAL_DIR, la carpeta local montada en el servidor); si no hay, se omite.
"""
import hashlib
import os
import shutil
from pathlib import Path

import pytest

from app import storage
from app.core.config import get_settings
from tests.test_documents import roles  # noqa: F401  (fixture)
from tests.test_extraction import REFERENCE_PDF

ASYNC = pytest.mark.asyncio(loop_scope="session")

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


def _redis_available() -> bool:
    try:
        import redis
        redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


@pytest.fixture
def memory_backend(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "storage_backend", "memory")
    monkeypatch.setattr(s, "redis_url", REDIS_URL)
    monkeypatch.setattr(s, "upload_ttl_seconds", 300)
    monkeypatch.setattr(storage, "_uploads", None)
    monkeypatch.setattr(storage, "_cache", storage._LruCache())
    yield
    monkeypatch.setattr(storage, "_uploads", None)


@pytest.mark.skipif(not _redis_available(), reason="Redis de prueba no disponible")
def test_memory_upload_is_temporary_and_verified(memory_backend):
    data = b"%PDF-1.7 prueba " + os.urandom(16)
    sha = hashlib.sha256(data).hexdigest()
    key = storage.keep_upload(sha, data)
    assert key == f"memory/{sha}" and storage.describe_source(key) == "upload"
    assert storage.read_original(key, sha) == data
    ttl = storage._upload_storage().client.ttl(f"na:pdf:{sha}")
    assert 0 < ttl <= 300                          # vence solo: nada queda guardado
    storage._cache = storage._LruCache()
    assert storage.read_original(key, "0" * 64) is None   # otra huella: no se usa
    storage.release_upload(key, sha)
    storage._cache = storage._LruCache()
    assert storage.read_original(key, sha) is None        # descartada tras procesar


def test_unresolved_pipeline_macro_is_empty(monkeypatch):
    from app.core.config import Settings
    monkeypatch.setenv("SFTP_HOST_KEY", "$(NEWS_ANALYZE_SFTP_HOST_KEY)")
    monkeypatch.setenv("SFTP_HOST", "$(NEWS_ANALYZE_SFTP_HOST)")
    s = Settings()
    assert s.sftp_host_key == "" and s.sftp_host == ""
    from app import sftp
    monkeypatch.setattr("app.sftp.get_settings", lambda: s)
    assert sftp.configured() is False


def test_invalid_storage_key_rejected():
    with pytest.raises(ValueError):
        storage.read_original("../../etc/passwd", "a" * 64)


@ASYNC
async def test_health_probes(roles):
    client = roles["admin"]
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).status_code == 200


# ---------------- SFTP (servidor de prueba) ----------------
SFTP = {k: os.environ.get(f"SFTP_TEST_{k.upper()}", "") for k in ("host", "port", "user", "password", "path", "local_dir")}
needs_sftp = pytest.mark.skipif(not (SFTP["host"] and SFTP["local_dir"] and REFERENCE_PDF.exists()),
                                reason="Servidor SFTP de prueba no configurado")


@pytest.fixture
def sftp_settings(monkeypatch):
    s = get_settings()
    for k, v in {"sftp_host": SFTP["host"], "sftp_port": int(SFTP["port"] or 22), "sftp_user": SFTP["user"],
                 "sftp_password": SFTP["password"], "sftp_path": SFTP["path"] or "/", "sftp_host_key": ""}.items():
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(storage, "_cache", storage._LruCache())
    local = Path(SFTP["local_dir"])
    for f in local.glob("*.pdf"):
        f.unlink()
    yield local
    for f in local.glob("*.pdf"):
        f.unlink()


@needs_sftp
@ASYNC
async def test_sftp_ingest_reads_in_place_and_viewer_rereads_origin(roles, sftp_settings):
    from app.agents.sftp_ingest import run_once
    from app.core.db import get_db
    local: Path = sftp_settings
    admin = roles["admin"]

    # Si otra prueba ya cargó el PDF de referencia, se usa una copia con un byte distinto al final (otra huella).
    data = REFERENCE_PDF.read_bytes() + b"\n%sftp-test\n"
    shutil.copyfile(REFERENCE_PDF, local / "resumen_sftp.pdf")
    (local / "resumen_sftp.pdf").write_bytes(data)
    remote = f"{(SFTP['path'] or '/').rstrip('/')}/resumen_sftp.pdf"

    first = await run_once()
    assert first["waiting"] == 1 and not first["processed"]      # primera vista: espera a que esté estable
    second = await run_once()
    assert len(second["processed"]) == 1, second
    doc_id = second["processed"][0]["document_id"]
    third = await run_once()
    assert third["skipped"] == 1                                  # no se vuelve a descargar ni a procesar

    doc = await get_db()["documents"].find_one({"id": doc_id})
    assert doc["storage_key"] == f"sftp:{remote}" and doc["ingest_source"] == "sftp" and doc["status"].startswith("completed")
    assert (local / "resumen_sftp.pdf").exists()                  # el original no se movió ni se borró

    r = await admin.get(f"/api/v1/documents/{doc_id}")
    assert r.json()["document"]["original_source"] == "sftp"
    r = await admin.get(f"/api/v1/documents/{doc_id}/pages/1/image")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"

    # Si el original cambia en el origen, el visor no lo usa.
    (local / "resumen_sftp.pdf").write_bytes(data + b"modificado")
    storage._cache = storage._LruCache()
    assert (await admin.get(f"/api/v1/documents/{doc_id}/pages/1/image")).status_code == 404
