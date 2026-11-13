"""Libro de auditoría (colección audit_logs), compatible bit a bit con lib/audit.ts de Next.js.

record_hash = HMAC-SHA256(canonical(registro sin _id ni record_hash)), secuencia única con reintento.
Ambas aplicaciones escriben en la misma cadena durante la transición; cualquiera de las dos la verifica.
El código solo inserta y lee: no hay actualización ni borrado.
"""
import asyncio
import hashlib
import hmac
import random
import json
import logging
import re
import uuid
from typing import Any

from pymongo.errors import DuplicateKeyError

from app.core.config import get_settings
from app.core.db import get_db, iso_now

log = logging.getLogger("audit")

GENESIS_HASH = "GENESIS_NEWS_ANALIZE"
KEY_VERSION = "v1"
_SENSITIVE = re.compile(r"password|passwd|secret|token|authorization|api[_-]?key|mongodb_uri|connection_string", re.I)


def canonical_json(value: Any) -> str:
    """Equivalente a canonicalJson() de TypeScript: claves ordenadas, sin espacios, sin escapar no-ASCII."""
    if isinstance(value, dict):
        items = sorted((k, v) for k, v in value.items() if v is not _UNDEFINED)
        return "{" + ",".join(f"{json.dumps(k, ensure_ascii=False)}:{canonical_json(v)}" for k, v in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return json.dumps(value)
    return json.dumps(str(value), ensure_ascii=False)


class _Undefined:  # marcador interno
    pass


_UNDEFINED = _Undefined()


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return value
    if isinstance(value, dict):
        return {k: ("[REDACTADO]" if _SENSITIVE.search(k) else _redact(v, depth + 1)) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_redact(v, depth + 1) for v in value]
    return value


def _hash(record: dict) -> str:
    key = get_settings().hmac_audit_secret.encode("utf-8")
    return hmac.new(key, canonical_json(record).encode("utf-8"), hashlib.sha256).hexdigest()


async def write_audit(
    *,
    user_id: str,
    user_email: str,
    user_roles: list[str],
    action: str,
    entity_type: str,
    entity_id: str,
    status: str,
    ip_address: str | None = None,
    request_id: str | None = None,
    details: dict | None = None,
    error_message: str | None = None,
) -> dict | None:
    col = get_db()["audit_logs"]
    for attempt in range(15):
        last = await col.find_one({}, projection={"seq_num": 1, "record_hash": 1}, sort=[("seq_num", -1)])
        record: dict[str, Any] = {
            "seq_num": (last["seq_num"] + 1) if last else 1,
            "event_id": str(uuid.uuid4()),
            "timestamp_utc": iso_now(),
            "user_id": user_id,
            "user_email": user_email,
            "user_roles": list(user_roles),
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "status": status,
            "ip_address": ip_address or "desconocida",
            "request_id": request_id or f"req_{uuid.uuid4()}",
            "prev_record_hash": last["record_hash"] if last else GENESIS_HASH,
            "key_version": KEY_VERSION,
        }
        if details:
            record["details"] = _redact(details)
        if error_message:
            record["error_message"] = error_message
        record["record_hash"] = _hash(record)
        try:
            await col.insert_one(dict(record))
            return record
        except DuplicateKeyError:
            # Otra escritura tomó la misma secuencia: espera aleatoria corta para no volver a chocar.
            await asyncio.sleep(random.uniform(0.005, 0.05) * (attempt + 1))
            continue
        except Exception:  # la auditoría nunca debe tumbar la petición
            log.exception("fallo al insertar auditoría %s", action)
            return None
    log.error("no se pudo reservar secuencia de auditoría para %s", action)
    return None


class AuditUnavailable(Exception):
    """La acción no se ejecuta si su registro de auditoría no pudo escribirse (descargas, exportaciones)."""


async def write_audit_required(**kwargs) -> dict:
    record = await write_audit(**kwargs)
    if record is None:
        raise AuditUnavailable(kwargs.get("action", ""))
    return record


def _check_batch(batch: list[dict], prev_hash: str, expected: int) -> tuple[bool, str, int, int | None, str]:
    """Verifica un lote (CPU: HMAC) fuera del event loop. Devuelve (ok, prev_hash, expected, seq_roto, motivo)."""
    for doc in batch:
        record_hash = doc.pop("record_hash", None)
        seq = doc.get("seq_num")
        if seq != expected:
            return False, prev_hash, expected, seq, f"Falta la secuencia {expected}"
        if doc.get("prev_record_hash") != prev_hash:
            return False, prev_hash, expected, seq, "Enlace con el registro anterior roto"
        if not hmac.compare_digest(_hash(doc), record_hash or ""):
            return False, prev_hash, expected, seq, "Contenido alterado (HMAC no coincide)"
        prev_hash = record_hash
        expected += 1
    return True, prev_hash, expected, None, ""


def _anchor_signature(seq: int, record_hash: str, at: str) -> str:
    key = get_settings().hmac_audit_secret.encode("utf-8")
    return hmac.new(key, f"anchor:{seq}:{record_hash}:{at}".encode(), hashlib.sha256).hexdigest()


async def write_anchor() -> dict | None:
    """Ancla de la cabeza de la cadena (seq + hash firmados). Detecta que se borren los últimos registros:
    el ancla también se escribe en el log del worker (fuera de MongoDB)."""
    head = await get_db()["audit_logs"].find_one({}, projection={"_id": 0, "seq_num": 1, "record_hash": 1}, sort=[("seq_num", -1)])
    if not head:
        return None
    at = iso_now()
    anchor = {"seq_num": head["seq_num"], "record_hash": head["record_hash"], "anchored_at": at,
              "signature": _anchor_signature(head["seq_num"], head["record_hash"], at)}
    await get_db()["audit_anchors"].insert_one(dict(anchor))
    log.info("AUDIT_ANCHOR seq=%s hash=%s at=%s", anchor["seq_num"], anchor["record_hash"], at)
    return anchor


async def verify_chain(full: bool = False) -> dict:
    """Verifica la cadena. Incremental desde el último punto verificado (rápido); `full` la recorre entera.
    Además comprueba las anclas: si la cadena es más corta que un ancla, se borraron registros del final."""
    db = get_db()
    col = db["audit_logs"]
    checkpoint = None if full else await db["system_configs"].find_one({"key": "audit.checkpoint"})
    prev_hash, expected = GENESIS_HASH, 1
    verified_before = 0
    if checkpoint:
        prev_hash, expected = checkpoint["record_hash"], checkpoint["seq_num"] + 1
        verified_before = checkpoint["seq_num"]
        cp_doc = await col.find_one({"seq_num": checkpoint["seq_num"]}, projection={"record_hash": 1})
        if not cp_doc or cp_doc.get("record_hash") != checkpoint["record_hash"]:
            return {"ok": False, "verified_count": 0, "first_broken_seq": checkpoint["seq_num"],
                    "reason": "El registro del último punto verificado fue alterado o borrado"}

    count = 0
    batch: list[dict] = []
    async for doc in col.find({"seq_num": {"$gte": expected}}, projection={"_id": 0}).sort("seq_num", 1):
        batch.append(doc)
        if len(batch) >= 1000:
            ok, prev_hash, expected, broken, reason = await asyncio.to_thread(_check_batch, batch, prev_hash, expected)
            if not ok:
                return {"ok": False, "verified_count": verified_before + count, "first_broken_seq": broken, "reason": reason}
            count += len(batch)
            batch = []
    if batch:
        ok, prev_hash, expected, broken, reason = await asyncio.to_thread(_check_batch, batch, prev_hash, expected)
        if not ok:
            return {"ok": False, "verified_count": verified_before + count, "first_broken_seq": broken, "reason": reason}
        count += len(batch)

    head_seq = expected - 1
    # El último ancla firmada debe coincidir con el registro de esa secuencia (si se borró el final y se
    # volvió a escribir encima, el hash ya no es el mismo).
    async for anchor in db["audit_anchors"].find({"seq_num": {"$lte": head_seq}}).sort("seq_num", -1).limit(1):
        valid = hmac.compare_digest(anchor.get("signature", ""), _anchor_signature(anchor["seq_num"], anchor["record_hash"], anchor["anchored_at"]))
        at = await col.find_one({"seq_num": anchor["seq_num"]}, projection={"record_hash": 1})
        if valid and (not at or at.get("record_hash") != anchor["record_hash"]):
            return {"ok": False, "verified_count": verified_before + count, "first_broken_seq": anchor["seq_num"],
                    "reason": f"Faltan registros al final de la cadena: el registro {anchor['seq_num']} no es el que registró el ancla firmada"}
    async for anchor in db["audit_anchors"].find({"seq_num": {"$gt": head_seq}}).sort("seq_num", -1).limit(1):
        if hmac.compare_digest(anchor.get("signature", ""), _anchor_signature(anchor["seq_num"], anchor["record_hash"], anchor["anchored_at"])):
            return {"ok": False, "verified_count": verified_before + count, "first_broken_seq": head_seq + 1,
                    "reason": f"Faltan registros al final de la cadena: un ancla firmada registra la secuencia {anchor['seq_num']}"}
    if head_seq > 0:
        await db["system_configs"].update_one(
            {"key": "audit.checkpoint"},
            {"$set": {"key": "audit.checkpoint", "seq_num": head_seq, "record_hash": prev_hash, "verified_at": iso_now()}},
            upsert=True)
    return {"ok": True, "verified_count": verified_before + count, "first_broken_seq": None,
            "incremental": bool(checkpoint), "head_seq": head_seq}
