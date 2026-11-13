"""Inicialización idempotente al arrancar: índices, matriz inicial de roles y administrador inicial."""
import logging
import uuid

from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.db import iso_now
from app.core.passwords import hash_password, password_policy_error
from app.core.rbac import CLAIM_MIGRATIONS, ROLE_CLAIMS, SYSTEM_ROLES

log = logging.getLogger("bootstrap")


async def run_bootstrap(db: AsyncDatabase) -> None:
    await db["users"].create_index([("email", ASCENDING)], unique=True)
    await db["users"].create_index([("id", ASCENDING)], unique=True)
    await db["roles"].create_index([("code", ASCENDING)], unique=True)
    await db["user_sessions"].create_index([("session_id", ASCENDING)], unique=True)
    await db["user_sessions"].create_index([("refresh_token_hash", ASCENDING)], sparse=True)
    await db["user_sessions"].create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
    await db["audit_logs"].create_index([("seq_num", ASCENDING)], unique=True)
    await db["audit_logs"].create_index([("timestamp_utc", DESCENDING)])
    await db["audit_logs"].create_index([("entity_type", ASCENDING), ("entity_id", ASCENDING)])
    await db["audit_logs"].create_index([("user_id", ASCENDING)])

    # Dominio documental y periodístico (Plan V3.0 §4.6)
    await db["documents"].create_index([("id", ASCENDING)], unique=True)
    await db["documents"].create_index([("file_hash", ASCENDING)], unique=True)
    await db["documents"].create_index([("status", ASCENDING), ("created_at", DESCENDING)])
    await db["documents"].create_index([("summary_date", DESCENDING)])
    await db["news"].create_index([("id", ASCENDING)], unique=True)
    await db["news"].create_index([("document_id", ASCENDING), ("news_index", ASCENDING)], unique=True)
    await db["news"].create_index([("summary_date", DESCENDING), ("media_name", ASCENDING)])
    await db["news"].create_index([("created_at", DESCENDING)])
    await db["news"].create_index([("is_duplicate", ASCENDING), ("canonical_news_id", ASCENDING)])
    await db["news"].create_index([("article_section", ASCENDING)])
    await db["news"].create_index([("review_status", ASCENDING)])
    await db["news_reviews"].create_index([("news_id", ASCENDING), ("timestamp_utc", DESCENDING)])
    await db["processing_jobs"].create_index([("id", ASCENDING)], unique=True)
    await db["processing_jobs"].create_index([("idempotency_key", ASCENDING)], unique=True)
    await db["processing_jobs"].create_index([("status", ASCENDING), ("lease_expires_at", ASCENDING)])
    await db["processing_jobs"].create_index([("document_id", ASCENDING), ("job_type", ASCENDING)])
    await db["outbox_events"].create_index([("id", ASCENDING)], unique=True)
    await db["outbox_events"].create_index([("status", ASCENDING), ("created_at", ASCENDING)])
    await db["system_configs"].create_index([("key", ASCENDING)], unique=True)

    now = iso_now()
    for code in SYSTEM_ROLES:
        await db["roles"].update_one(
            {"code": code},
            {"$setOnInsert": {"code": code, "claims": list(ROLE_CLAIMS[code]), "is_system": True, "created_at": now}},
            upsert=True,
        )

    for key, grants in CLAIM_MIGRATIONS:
        marker = f"migration.{key}"
        if await db["system_configs"].find_one({"key": marker}):
            continue
        for code, claims in grants.items():
            await db["roles"].update_one({"code": code}, {"$addToSet": {"claims": {"$each": claims}}})
        await db["system_configs"].update_one({"key": marker}, {"$set": {"key": marker, "applied_at": now}}, upsert=True)
        log.info("Migración de claims aplicada: %s", key)

    if await db["users"].count_documents({}, limit=1) > 0:
        return

    s = get_settings()
    email = s.bootstrap_admin_email.strip().lower()
    password = s.bootstrap_admin_password
    if not email or not password:
        log.warning("No hay usuarios y faltan BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD: nadie podrá iniciar sesión.")
        return
    if err := password_policy_error(password):
        log.warning("BOOTSTRAP_ADMIN_PASSWORD no cumple la política: %s", err)
        return

    admin_id = f"usr_{uuid.uuid4()}"
    try:
        await db["users"].insert_one({
            "id": admin_id,
            "email": email,
            "name": "Administrador inicial",
            "roles": ["admin"],
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "created_by": "bootstrap",
            "password_hash": hash_password(password),
            "must_change_password": True,
            "failed_attempts": 0,
            "locked_until": None,
        })
    except DuplicateKeyError:
        return
    log.info("Administrador inicial creado: %s (debe cambiar la contraseña al entrar).", email)
    await write_audit(
        user_id="bootstrap", user_email="sistema", user_roles=[], action="BOOTSTRAP_ADMIN_CREATED",
        entity_type="USER", entity_id=admin_id, status="SUCCESS", details={"email": email},
    )
