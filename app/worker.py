"""Cola de trabajos: Celery + Redis como única pila (Plan V3.0 §6.1).

Arranque del worker (Windows de desarrollo necesita --pool=solo):
    celery -A app.worker worker --beat --pool=solo --loglevel=info

Tareas:
  process_document  → extrae un documento (reintentos con espera exponencial 30 s / 2 min / 8 min)
  dispatch_outbox   → cada 5 s publica eventos PENDING (garantiza que un documento registrado siempre genera trabajo)
  reconcile_jobs    → cada 60 s devuelve a la cola los trabajos con concesión vencida
  ingest_sftp       → cada SFTP_POLL_SECONDS toma los PDF nuevos del file server (si SFTP está configurado)

En AKS el worker lleva el programador incluido (una sola réplica): celery -A app.worker worker -B
"""
import asyncio
import logging

from celery import Celery

from app.core import db
from app.core.config import get_settings
from app.core.db import get_db, iso_now

log = logging.getLogger("worker")

_s = get_settings()
celery_app = Celery("news_analyze", broker=_s.broker_url)
celery_app.conf.update(
    task_acks_late=True,                 # si el worker cae a mitad, el mensaje se reentrega
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="news_analyze",
    beat_schedule={
        "dispatch-outbox": {"task": "app.worker.dispatch_outbox", "schedule": 5.0},
        "reconcile-jobs": {"task": "app.worker.reconcile_jobs", "schedule": 60.0},
        "ingest-sftp": {"task": "app.worker.ingest_sftp", "schedule": float(_s.sftp_poll_seconds)},
    },
)


async def publish_pending_events(limit: int = 50) -> int:
    """Publica eventos del outbox. En modo síncrono (pruebas) ejecuta el trabajo en el acto."""
    from app.services.pipeline import process_job

    col = get_db()["outbox_events"]
    published = 0
    for _ in range(limit):
        event = await col.find_one_and_update(
            {"status": "PENDING"}, {"$set": {"status": "PUBLISHING"}, "$inc": {"attempts": 1}}, sort=[("created_at", 1)])
        if not event:
            break
        job_id = event["payload"]["job_id"]
        try:
            if get_settings().celery_always_eager:
                await process_job(job_id)
                task_id = "eager"
            elif get_settings().broker_url.startswith("memory://"):
                # Sin Redis no hay worker que reciba el trabajo: el evento queda pendiente (no se pierde).
                log.warning("REDIS_URL no configurado: el trabajo %s queda en cola hasta que haya broker", job_id)
                await col.update_one({"id": event["id"]}, {"$set": {"status": "PENDING"}})
                break
            else:
                task_id = celery_app.send_task("app.worker.process_document", args=[job_id]).id
            await col.update_one({"id": event["id"]}, {"$set": {"status": "PUBLISHED", "published_at": iso_now()}})
            await get_db()["processing_jobs"].update_one({"id": job_id}, {"$set": {"celery_task_id": task_id}})
            published += 1
        except Exception:
            log.exception("no se pudo publicar %s", event["id"])
            await col.update_one({"id": event["id"]}, {"$set": {"status": "PENDING" if event["attempts"] < 20 else "FAILED"}})
            break
    return published


def _run(coro_factory):
    async def wrapper():
        await db.connect()
        try:
            return await coro_factory()
        finally:
            await db.close()
    return asyncio.run(wrapper())


@celery_app.task(name="app.worker.process_document", bind=True, max_retries=None)
def process_document(self, job_id: str):
    from app.services.pipeline import RETRY_BACKOFF_SECONDS, process_job
    outcome = _run(lambda: process_job(job_id))
    if outcome == "retry":
        delay = RETRY_BACKOFF_SECONDS[min(self.request.retries, len(RETRY_BACKOFF_SECONDS) - 1)]
        raise self.retry(countdown=delay)
    return outcome


@celery_app.task(name="app.worker.dispatch_outbox")
def dispatch_outbox():
    # Un evento atascado en PUBLISHING (proceso caído) vuelve a PENDING tras 2 minutos.
    async def go():
        from datetime import timedelta
        from app.core.db import utcnow
        stale = (utcnow() - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        await get_db()["outbox_events"].update_many(
            {"status": "PUBLISHING", "created_at": {"$lt": stale}}, {"$set": {"status": "PENDING"}})
        return await publish_pending_events()
    return _run(go)


@celery_app.task(name="app.worker.reconcile_jobs")
def reconcile_jobs():
    from app.services.pipeline import reconcile_expired_jobs
    return _run(reconcile_expired_jobs)


@celery_app.task(name="app.worker.ingest_sftp")
def ingest_sftp():
    from app import sftp
    if not sftp.configured():
        return {"disabled": True}
    from app.agents.sftp_ingest import run_once
    return _run(run_once)
