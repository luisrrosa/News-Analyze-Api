"""Libro de auditoría (colección audit_logs), compatible bit a bit con lib/audit.ts de Next.js.

record_hash = HMAC-SHA256(canonical(registro sin _id ni record_hash)), secuencia única con reintento.
Ambas aplicaciones escriben en la misma cadena durante la transición; cualquiera de las dos la verifica.
El código solo inserta y lee: no hay actualización ni borrado.
"""
import hashlib
import hmac
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
    for _ in range(10):
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
            continue  # otra escritura tomó la misma secuencia
        except Exception:  # la auditoría nunca debe tumbar la petición
            log.exception("fallo al insertar auditoría %s", action)
            return None
    log.error("no se pudo reservar secuencia de auditoría para %s", action)
    return None


async def verify_chain() -> dict:
    col = get_db()["audit_logs"]
    prev_hash = GENESIS_HASH
    expected = 1
    count = 0
    async for doc in col.find({}).sort("seq_num", 1):
        doc.pop("_id", None)
        record_hash = doc.pop("record_hash", None)
        seq = doc.get("seq_num")
        if seq != expected:
            return {"ok": False, "verified_count": count, "first_broken_seq": seq, "reason": f"Falta la secuencia {expected}"}
        if doc.get("prev_record_hash") != prev_hash:
            return {"ok": False, "verified_count": count, "first_broken_seq": seq, "reason": "Enlace con el registro anterior roto"}
        if not hmac.compare_digest(_hash(doc), record_hash or ""):
            return {"ok": False, "verified_count": count, "first_broken_seq": seq, "reason": "Contenido alterado (HMAC no coincide)"}
        prev_hash = record_hash
        expected += 1
        count += 1
    return {"ok": True, "verified_count": count, "first_broken_seq": None}
