"""Agente de ingesta por carpeta (Plan V3.0 §15, decisión D-05).

    python -m app.agents.folder_ingest            # bucle continuo
    python -m app.agents.folder_ingest --once     # una pasada (pruebas / tareas programadas)

- Solo lee la carpeta configurada (INGEST_FOLDER); el navegador no envía rutas.
- Un archivo se procesa cuando su tamaño y fecha no cambian entre dos lecturas separadas INGEST_STABLE_SECONDS
  (evita tomar un archivo a medio copiar).
- No sigue enlaces simbólicos ni rutas que salgan de la carpeta.
- Tras procesarlo lo mueve a procesados/ o rechazados/ (con un .motivo.txt) y audita con origen "folder".
"""
import argparse
import asyncio
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from app.core import db
from app.core.audit import write_audit
from app.core.bootstrap import run_bootstrap
from app.core.config import get_settings
from app.services.documents import Actor, DocumentRejected, receive_document

log = logging.getLogger("folder_ingest")
SERVICE_ACTOR = Actor(user_id="agent:folder", user_email="agente-ingesta", user_roles=[])


def _dirs() -> tuple[Path, Path, Path]:
    root = Path(get_settings().ingest_folder).resolve()
    ok, bad = root / "procesados", root / "rechazados"
    for d in (root, ok, bad):
        d.mkdir(parents=True, exist_ok=True)
    return root, ok, bad


def _unique_target(folder: Path, name: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return folder / f"{stamp}_{name}"


def stable_candidates(root: Path, wait_seconds: float) -> list[Path]:
    """PDFs regulares dentro de la carpeta cuyo tamaño y fecha no cambian durante wait_seconds."""
    def snapshot() -> dict[Path, tuple[int, float]]:
        out = {}
        for p in root.iterdir():
            if p.is_symlink() or not p.is_file() or p.suffix.lower() != ".pdf":
                continue
            if p.resolve().parent != root:  # fuera de la carpeta permitida
                continue
            st = p.stat()
            out[p] = (st.st_size, st.st_mtime)
        return out

    first = snapshot()
    if not first:
        return []
    time.sleep(wait_seconds)
    second = snapshot()
    return [p for p, sig in first.items() if second.get(p) == sig and sig[0] > 0]


async def run_once(wait_seconds: float | None = None) -> dict:
    root, ok_dir, bad_dir = _dirs()
    wait = get_settings().ingest_stable_seconds if wait_seconds is None else wait_seconds
    summary = {"processed": [], "rejected": [], "symlinks_ignored": []}

    for p in root.iterdir():
        if p.is_symlink():
            summary["symlinks_ignored"].append(p.name)
            await write_audit(**_actor(), action="FOLDER_INGEST_REJECTED", entity_type="FILE", entity_id=p.name,
                              status="DENIED", error_message="Enlace simbólico no permitido",
                              details={"origin": "folder", "relative_path": p.name})

    for path in await asyncio.to_thread(stable_candidates, root, wait):
        rel = path.name
        try:
            doc = await receive_document(path.read_bytes(), rel, SERVICE_ACTOR, source="folder")
            shutil.move(str(path), _unique_target(ok_dir, rel))
            summary["processed"].append({"file": rel, "document_id": doc["id"]})
            await write_audit(**_actor(), action="DOC_UPLOADED", entity_type="DOCUMENT", entity_id=doc["id"],
                              status="SUCCESS", details={"file_name": rel, "sha256": doc["file_hash"], "origin": "folder",
                                                         "relative_path": rel})
        except DocumentRejected as e:
            target = _unique_target(bad_dir, rel)
            shutil.move(str(path), target)
            target.with_name(target.name + ".motivo.txt").write_text(f"{e.code}: {e.message}\n", encoding="utf-8")
            summary["rejected"].append({"file": rel, "reason": e.code})
            await write_audit(**_actor(), action="FOLDER_INGEST_REJECTED", entity_type="FILE", entity_id=rel,
                              status="DENIED", error_message=e.message,
                              details={"origin": "folder", "code": e.code, "existing_document": e.existing_id})
    return summary


def _actor() -> dict:
    return {"user_id": SERVICE_ACTOR.user_id, "user_email": SERVICE_ACTOR.user_email, "user_roles": []}


async def main(once: bool) -> None:
    database = await db.connect()
    await run_bootstrap(database)
    try:
        while True:
            result = await run_once()
            if result["processed"] or result["rejected"]:
                log.info("ingesta por carpeta: %s", result)
            if once:
                break
            await asyncio.sleep(get_settings().ingest_poll_seconds)
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    asyncio.run(main(parser.parse_args().once))
