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
        # La API publica al recibir el documento; esto solo recoge lo que quedó pendiente.
        "dispatch-outbox": {"task": "app.worker.dispatch_outbox", "schedule": 15.0},
        "reconcile-jobs": {"task": "app.worker.reconcile_jobs", "schedule": 60.0},
        "ingest-sftp": {"task": "app.worker.ingest_sftp", "schedule": float(_s.sftp_poll_seconds)},
        "audit-anchor": {"task": "app.worker.audit_anchor", "schedule": 3600.0},
        "audit-verify-full": {"task": "app.worker.audit_verify_full", "schedule": 86400.0},
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


_loop: asyncio.AbstractEventLoop | None = None


def _run(coro_factory):
    """Un event loop y un cliente MongoDB por proceso del worker (antes: uno nuevo por tarea, cada 5 s)."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _loop.run_until_complete(db.connect())
    return _loop.run_until_complete(coro_factory())


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


@celery_app.task(name="app.worker.audit_anchor")
def audit_anchor():
    """Cada hora: ancla firmada de la cabeza de la cadena (también queda en el log, fuera de MongoDB)."""
    from app.core.audit import write_anchor
    anchor = _run(write_anchor)
    return {"seq_num": anchor["seq_num"]} if anchor else None


@celery_app.task(name="app.worker.audit_verify_full")
def audit_verify_full():
    """Cada día: verificación completa de la cadena (la pantalla hace la incremental)."""
    from app.core.audit import verify_chain
    result = _run(lambda: verify_chain(full=True))
    if not result["ok"]:
        log.error("AUDIT_CHAIN_BROKEN seq=%s motivo=%s", result["first_broken_seq"], result.get("reason"))
    return {"ok": result["ok"], "verified": result["verified_count"]}


def enqueue_topic_recompute() -> bool:
    """Encola el recálculo de temas en el worker. False si no hay broker (pruebas/síncrono): lo hace quien llama."""
    s = get_settings()
    if s.celery_always_eager or s.broker_url.startswith("memory://"):
        return False
    celery_app.send_task("app.worker.recompute_topics")
    return True


@celery_app.task(name="app.worker.recompute_topics", bind=True, max_retries=None)
def recompute_topics(self):
    """Un solo recálculo a la vez (candado en MongoDB). Si hay otro en curso, se reintenta después para aplicar
    la versión más reciente de las reglas."""
    async def go():
        from datetime import timedelta
        from app.core.db import utcnow
        from app.services.topics import recompute_news_topics
        col = get_db()["system_configs"]
        now = utcnow()
        lock = await col.find_one_and_update(
            {"key": "lock.recompute_topics", "$or": [{"until": {"$lt": now}}, {"until": None}]},
            {"$set": {"until": now + timedelta(minutes=30)}}, upsert=False)
        if lock is None:
            try:
                await col.insert_one({"key": "lock.recompute_topics", "until": now + timedelta(minutes=30)})
            except Exception:
                return "busy"
        try:
            return await recompute_news_topics()
        finally:
            await col.update_one({"key": "lock.recompute_topics"}, {"$set": {"until": None}})
    result = _run(go)
    if result == "busy":
        raise self.retry(countdown=60)
    return result
