"""Ingesta desde el file server por SFTP (decisión del propietario para AKS).

El original NO se mueve, no se copia ni se borra: cada PDF nuevo se lee en memoria, se procesa y el documento
guarda su ruta (`storage_key = sftp:<ruta>`) para que el visor lo vuelva a leer del origen.

- Un archivo se toma cuando su tamaño y fecha coinciden con los de la revisión anterior (no se toma a medio copiar).
- Lo ya visto se recuerda en `sftp_seen` (ruta + tamaño + fecha): no se vuelve a descargar en cada revisión.
- Un PDF ya cargado (misma huella SHA-256, venga de donde venga) se registra como duplicado y no se reprocesa.

Lo ejecuta el worker cada SFTP_POLL_SECONDS (Celery beat) o a mano:
    python -m app.agents.sftp_ingest --once
"""
import argparse
import asyncio
import logging

from app.core import db
from app.core.audit import write_audit
from app.core.bootstrap import run_bootstrap
from app.core.db import get_db, iso_now
from app.services.documents import Actor, DocumentRejected, receive_document
from app.storage import sftp_key

log = logging.getLogger("sftp_ingest")
SERVICE_ACTOR = Actor(user_id="agent:sftp", user_email="agente-sftp", user_roles=[])


def _actor() -> dict:
    return {"user_id": SERVICE_ACTOR.user_id, "user_email": SERVICE_ACTOR.user_email, "user_roles": []}


async def run_once() -> dict:
    from app import sftp
    summary = {"processed": [], "rejected": [], "waiting": 0, "skipped": 0}
    if not sftp.configured():
        return {**summary, "disabled": True}

    files = await asyncio.to_thread(sftp.list_pdfs)
    seen = get_db()["sftp_seen"]
    for f in files:
        record = await seen.find_one({"path": f.path})
        signature = {"size": f.size, "mtime": f.mtime}
        if record and record.get("size") == f.size and record.get("mtime") == f.mtime and record.get("status") in ("processed", "rejected"):
            summary["skipped"] += 1
            continue
        if not record or record.get("size") != f.size or record.get("mtime") != f.mtime or f.size == 0:
            # Primera vez que se ve (o cambió): se espera a la siguiente revisión para confirmar que está completo.
            await seen.update_one({"path": f.path}, {"$set": {**signature, "status": "waiting", "updated_at": iso_now()}}, upsert=True)
            summary["waiting"] += 1
            continue

        try:
            data = await asyncio.to_thread(sftp.read_file, f.path)
            doc = await receive_document(data, f.name, SERVICE_ACTOR, source="sftp", storage_key=sftp_key(f.path))
            await seen.update_one({"path": f.path}, {"$set": {"status": "processed", "document_id": doc["id"], "updated_at": iso_now()}})
            summary["processed"].append({"file": f.path, "document_id": doc["id"]})
            await write_audit(**_actor(), action="DOC_UPLOADED", entity_type="DOCUMENT", entity_id=doc["id"], status="SUCCESS",
                              details={"file_name": f.name, "sha256": doc["file_hash"], "origin": "sftp", "remote_path": f.path})
        except DocumentRejected as e:
            await seen.update_one({"path": f.path}, {"$set": {"status": "rejected", "reason": e.code, "updated_at": iso_now()}})
            summary["rejected"].append({"file": f.path, "reason": e.code})
            await write_audit(**_actor(), action="SFTP_INGEST_REJECTED", entity_type="FILE", entity_id=f.path, status="DENIED",
                              error_message=e.message, details={"origin": "sftp", "code": e.code, "existing_document": e.existing_id})
        except Exception as e:  # un archivo con problemas no detiene la revisión; se reintenta en la próxima
            log.exception("no se pudo procesar %s", f.path)
            await seen.update_one({"path": f.path}, {"$set": {"status": "error", "reason": str(e)[:300], "updated_at": iso_now()}})
    return summary


async def main(once: bool) -> None:
    from app.core.config import get_settings
    database = await db.connect()
    await run_bootstrap(database)
    try:
        while True:
            result = await run_once()
            if result["processed"] or result["rejected"]:
                log.info("ingesta SFTP: %s", result)
            if once:
                break
            await asyncio.sleep(get_settings().sftp_poll_seconds)
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    asyncio.run(main(parser.parse_args().once))
