"""Usuarios y roles sobre MongoDB. Esquema compartido con la app Next.js (colecciones users y roles)."""
import uuid
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from app.core.db import get_db, iso_now
from app.core.passwords import hash_password
from app.core.rbac import ADMIN_LOCKED_CLAIMS, CLAIM_IDS, SYSTEM_ROLES

MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 15


class UserError(Exception):
    pass


def public_user(u: dict) -> dict:
    return {k: v for k, v in u.items() if k not in ("_id", "password_hash")}


async def find_by_email(email: str) -> dict | None:
    return await get_db()["users"].find_one({"email": email.strip().lower()})


async def find_by_id(user_id: str) -> dict | None:
    return await get_db()["users"].find_one({"id": user_id})


async def list_users() -> list[dict]:
    return [public_user(u) async for u in get_db()["users"].find({}).sort("created_at", 1)]


async def create_user(*, name: str, email: str, roles: list[str], password: str, created_by: str) -> dict:
    now = iso_now()
    user = {
        "id": f"usr_{uuid.uuid4()}",
        "email": email.strip().lower(),
        "name": name.strip(),
        "roles": roles,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "created_by": created_by,
        "password_hash": hash_password(password),
        "must_change_password": True,
        "failed_attempts": 0,
        "locked_until": None,
    }
    try:
        await get_db()["users"].insert_one(dict(user))
    except DuplicateKeyError as e:
        raise UserError(f"Ya existe un usuario registrado con el correo {user['email']}") from e
    return user


async def update_user(user_id: str, *, name: str | None, roles: list[str] | None, status: str | None) -> tuple[dict, dict]:
    col = get_db()["users"]
    before = await col.find_one({"id": user_id})
    if not before:
        raise UserError("Usuario no encontrado")
    changes: dict = {"updated_at": iso_now()}
    if name is not None:
        changes["name"] = name.strip()
    if roles is not None:
        changes["roles"] = roles
    if status is not None:
        changes["status"] = status
        if status == "active":
            changes.update(failed_attempts=0, locked_until=None)
    await col.update_one({"id": user_id}, {"$set": changes})
    if status is not None and status != "active":
        await revoke_all_sessions(user_id, "user_deactivated")
    after = await col.find_one({"id": user_id})
    return before, after  # type: ignore[return-value]


async def set_password(user_id: str, password_hash: str, must_change: bool) -> None:
    await get_db()["users"].update_one(
        {"id": user_id},
        {"$set": {"password_hash": password_hash, "must_change_password": must_change,
                  "failed_attempts": 0, "locked_until": None, "updated_at": iso_now()}},
    )


async def set_password_plain(user_id: str, password: str, must_change: bool) -> None:
    await set_password(user_id, hash_password(password), must_change)


def is_locked(u: dict) -> bool:
    until = u.get("locked_until")
    if not until:
        return False
    try:
        return datetime.fromisoformat(str(until).replace("Z", "+00:00")) > datetime.now(timezone.utc)
    except ValueError:
        return False


async def register_failed_login(u: dict) -> bool:
    """Incremento atómico: peticiones en paralelo no pueden saltarse el bloqueo (cada una suma 1 en la BD)."""
    from pymongo import ReturnDocument
    col = get_db()["users"]
    after = await col.find_one_and_update({"id": u["id"]}, {"$inc": {"failed_attempts": 1}},
                                          projection={"failed_attempts": 1}, return_document=ReturnDocument.AFTER)
    if not after or int(after.get("failed_attempts") or 0) < MAX_FAILED_ATTEMPTS:
        return False
    locked_until = (datetime.now(timezone.utc) + timedelta(minutes=LOCK_MINUTES)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    # Solo la petición que alcanza el umbral reinicia el contador y fija el bloqueo.
    res = await col.update_one({"id": u["id"], "failed_attempts": {"$gte": MAX_FAILED_ATTEMPTS}},
                               {"$set": {"failed_attempts": 0, "locked_until": locked_until}})
    return res.modified_count == 1


async def register_successful_login(u: dict) -> None:
    await get_db()["users"].update_one(
        {"id": u["id"]}, {"$set": {"failed_attempts": 0, "locked_until": None, "last_login": iso_now()}}
    )


# ---------- Roles ----------
async def role_claims_map() -> dict[str, list[str]]:
    result = {r: [] for r in SYSTEM_ROLES}
    async for d in get_db()["roles"].find({}):
        if d.get("code") in result:
            result[d["code"]] = list(d.get("claims") or [])
    return result


_ROLE_CACHE: dict = {"at": 0.0, "map": None}


def invalidate_role_cache() -> None:
    _ROLE_CACHE["map"] = None


async def resolve_claims(roles: list[str]) -> list[str]:
    import time
    m = _ROLE_CACHE["map"]
    if m is None or time.monotonic() - _ROLE_CACHE["at"] > 30:
        m = await role_claims_map()
        _ROLE_CACHE.update(at=time.monotonic(), map=m)
    return sorted({c for r in roles for c in m.get(r, [])})


async def set_role_claims(role: str, claims: list[str]) -> tuple[list[str], list[str]]:
    if role not in SYSTEM_ROLES:
        raise UserError(f"Rol desconocido: {role}")
    unknown = [c for c in claims if c not in CLAIM_IDS]
    if unknown:
        raise UserError(f"Claims desconocidos: {', '.join(unknown)}")
    if role == "admin":
        missing = [c for c in ADMIN_LOCKED_CLAIMS if c not in claims]
        if missing:
            raise UserError(f"El rol admin no puede perder: {', '.join(missing)}")
    col = get_db()["roles"]
    current = await col.find_one({"code": role})
    after = sorted(set(claims))
    await col.update_one({"code": role}, {"$set": {"claims": after, "updated_at": iso_now()}}, upsert=True)
    invalidate_role_cache()
    return list((current or {}).get("claims") or []), after


# ---------- Sesiones (también usadas por las rutas que siguen en Next.js) ----------
async def revoke_all_sessions(user_id: str, reason: str, except_session: str | None = None) -> None:
    query: dict = {"user_id": user_id, "revoked_at": None}
    if except_session:
        query["session_id"] = {"$ne": except_session}
    try:
        from app.api.deps import forget_session
        forget_session()
    except ImportError:
        pass
    await get_db()["user_sessions"].update_many(
        query,
        {"$set": {"revoked_at": datetime.now(timezone.utc), "revoked_reason": reason}},
    )
