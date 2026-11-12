"""Sesiones con refresh token rotativo (Plan V3.0 §7).

Cada sesión es una familia de tokens (session_id = token_family_id). Al refrescar:
- token vigente  → se marca consumido y se emite uno nuevo de la misma familia;
- token consumido hace ≤ REFRESH_GRACE_SECONDS → carrera legítima del cliente: se emite solo un access token
  (sin nuevo refresh, para no pisar la cookie que ya recibió la otra petición);
- token consumido hace más → reutilización: se revoca toda la familia. No se bloquea la cuenta (evita DoS).

Formato compatible con Next.js: session_id, user_id, expires_at (fecha), revoked_at.
"""
import uuid
from dataclasses import dataclass
from datetime import timedelta

from pymongo import ReturnDocument

from app.core.config import get_settings
from app.core.db import get_db, utcnow
from app.core.tokens import new_opaque_token, sha256


@dataclass
class RefreshResult:
    status: str  # "rotated" | "grace" | "reused" | "invalid"
    session_id: str | None = None
    user_id: str | None = None
    refresh_token: str | None = None


async def create_session(user_id: str, ip: str, user_agent: str) -> tuple[str, str]:
    s = get_settings()
    session_id = f"ses_{uuid.uuid4()}"
    refresh = new_opaque_token()
    now = utcnow()
    await get_db()["user_sessions"].insert_one({
        "session_id": session_id,
        "user_id": user_id,
        "refresh_token_hash": sha256(refresh),
        "consumed_tokens": [],
        "issued_at": now,
        "last_refresh_at": now,
        "expires_at": now + timedelta(days=s.refresh_token_expire_days),
        "revoked_at": None,
        "revoked_reason": None,
        "ip": ip,
        "user_agent": user_agent[:300],
    })
    return session_id, refresh


async def is_session_active(session_id: str, user_id: str) -> bool:
    doc = await get_db()["user_sessions"].find_one({"session_id": session_id, "user_id": user_id})
    return bool(doc) and doc.get("revoked_at") is None and doc["expires_at"] > utcnow()


async def rotate_refresh(refresh_token: str) -> RefreshResult:
    col = get_db()["user_sessions"]
    h = sha256(refresh_token)
    now = utcnow()
    new_token = new_opaque_token()

    # Rotación atómica: solo una petición puede consumir el token vigente.
    doc = await col.find_one_and_update(
        {"refresh_token_hash": h, "revoked_at": None, "expires_at": {"$gt": now}},
        {
            "$set": {"refresh_token_hash": sha256(new_token), "last_refresh_at": now},
            "$push": {"consumed_tokens": {"$each": [{"token_hash": h, "consumed_at": now}], "$slice": -50}},
        },
        return_document=ReturnDocument.AFTER,
    )
    if doc:
        return RefreshResult("rotated", doc["session_id"], doc["user_id"], new_token)

    consumed = await col.find_one({"consumed_tokens.token_hash": h})
    if not consumed:
        return RefreshResult("invalid")
    if consumed.get("revoked_at") is not None or consumed["expires_at"] <= now:
        return RefreshResult("invalid")

    entry = next(e for e in consumed["consumed_tokens"] if e["token_hash"] == h)
    age = (now - entry["consumed_at"]).total_seconds()
    if age <= get_settings().refresh_grace_seconds:
        return RefreshResult("grace", consumed["session_id"], consumed["user_id"])

    await revoke_session(consumed["session_id"], "refresh_token_reuse")
    return RefreshResult("reused", consumed["session_id"], consumed["user_id"])


async def revoke_session(session_id: str, reason: str) -> None:
    await get_db()["user_sessions"].update_one(
        {"session_id": session_id, "revoked_at": None},
        {"$set": {"revoked_at": utcnow(), "revoked_reason": reason}},
    )
