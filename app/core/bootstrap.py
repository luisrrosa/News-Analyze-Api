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
from app.core.rbac import CLAIM_IDS, CLAIM_MIGRATIONS, CLAIM_SPLITS, ROLE_CLAIMS, SYSTEM_ROLES

log = logging.getLogger("bootstrap")


async def run_bootstrap(db: AsyncDatabase) -> None:
    await db["users"].create_index([("email", ASCENDING)], unique=True)
    await db["users"].create_index([("id", ASCENDING)], unique=True)
    await db["roles"].create_index([("code", ASCENDING)], unique=True)
    await db["user_sessions"].create_index([("session_id", ASCENDING)], unique=True)
    await db["user_sessions"].create_index([("refresh_token_hash", ASCENDING)], sparse=True)
    await db["user_sessions"].create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
    await db["audit_logs"].create_index([("seq_num", ASCENDING)], unique=True)
    await db["sftp_seen"].create_index([("path", ASCENDING)], unique=True)
    await db["audit_logs"].create_index([("timestamp_utc", DESCENDING)])
    await db["audit_logs"].create_index([("entity_type", ASCENDING), ("entity_id", ASCENDING)])
    await db["audit_logs"].create_index([("user_id", ASCENDING)])
    await db["audit_logs"].create_index([("user_email", ASCENDING), ("seq_num", DESCENDING)])
    await db["audit_logs"].create_index([("timestamp_utc", DESCENDING), ("seq_num", DESCENDING)])
    await db["audit_anchors"].create_index([("seq_num", DESCENDING)])
    await db["viewer_access"].create_index([("key", ASCENDING)], unique=True)
    await db["viewer_access"].create_index([("created_at", ASCENDING)], expireAfterSeconds=7200)
    # Explorador, ficha y revisión (consultas reales): orden por defecto, relacionadas y cola.
    await db["news"].create_index([("summary_date", DESCENDING), ("document_id", ASCENDING), ("news_index", ASCENDING)])
    await db["news"].create_index([("canonical_news_id", ASCENDING)], sparse=True)
    await db["news"].create_index([("related_news_ids", ASCENDING)])
    await db["news"].create_index([("review_status", ASCENDING), ("summary_date", DESCENDING), ("news_index", ASCENDING)])
    await db["news"].create_index([("media_name", ASCENDING), ("summary_date", DESCENDING)])

    # Dominio documental y periodístico (Plan V3.0 §4.6)
    await db["documents"].create_index([("id", ASCENDING)], unique=True)
    await db["documents"].create_index([("file_hash", ASCENDING)], unique=True)
    await db["documents"].create_index([("status", ASCENDING), ("created_at", DESCENDING)])
    await db["documents"].create_index([("summary_date", DESCENDING)])
    await db["documents"].create_index([("updated_at", DESCENDING)])
    await db["news"].create_index([("updated_at", DESCENDING)])
    await db["news"].create_index([("id", ASCENDING)], unique=True)
    await db["news"].create_index([("document_id", ASCENDING), ("news_index", ASCENDING)], unique=True)
    await db["news"].create_index([("summary_date", DESCENDING), ("media_name", ASCENDING)])
    await db["news"].create_index([("created_at", DESCENDING)])
    await db["news"].create_index([("is_duplicate", ASCENDING), ("canonical_news_id", ASCENDING)])
    await db["news"].create_index([("article_section", ASCENDING)])
    await db["news"].create_index([("review_status", ASCENDING)])
    await db["news_reviews"].create_index([("news_id", ASCENDING), ("proposed_at", DESCENDING)])
    await db["news_reviews"].create_index([("id", ASCENDING)], unique=True, sparse=True)
    await db["news_reviews"].create_index([("status", ASCENDING), ("proposed_at", DESCENDING)])
    await db["news"].create_index([("review_status", ASCENDING), ("review_reasons", ASCENDING)])
    await db["processing_jobs"].create_index([("id", ASCENDING)], unique=True)
    await db["processing_jobs"].create_index([("idempotency_key", ASCENDING)], unique=True)
    await db["processing_jobs"].create_index([("status", ASCENDING), ("lease_expires_at", ASCENDING)])
    await db["processing_jobs"].create_index([("document_id", ASCENDING), ("job_type", ASCENDING)])
    await db["outbox_events"].create_index([("id", ASCENDING)], unique=True)
    await db["outbox_events"].create_index([("status", ASCENDING), ("created_at", ASCENDING)])
    await db["system_configs"].create_index([("key", ASCENDING)], unique=True)
    # Catálogos y reglas (Plan V3.0 §4.4)
    await db["rule_sets"].create_index([("version", ASCENDING)], unique=True)
    await db["rule_sets"].create_index([("status", ASCENDING), ("approved_at", DESCENDING)])
    await db["media_sources"].create_index([("id", ASCENDING)], unique=True)
    await db["media_sources"].create_index([("name", ASCENDING)], unique=True)
    await db["officeholders"].create_index([("id", ASCENDING)], unique=True)
    await db["officeholders"].create_index([("position", ASCENDING), ("term_start", DESCENDING)])

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

    # Claims divididos (un claim por acción): se agregan los específicos y se quitan los que ya no existen.
    for key, rules in CLAIM_SPLITS:
        marker = f"migration.{key}"
        if await db["system_configs"].find_one({"key": marker}):
            continue
        async for role in db["roles"].find({}):
            have = set(role.get("claims") or [])
            new = set(have)
            for required, granted in rules:
                if all(c in have for c in required):
                    new.update(granted)
            new = sorted(c for c in new if c in CLAIM_IDS)
            await db["roles"].update_one({"_id": role["_id"]}, {"$set": {"claims": new, "updated_at": now}})
        await db["system_configs"].update_one({"key": marker}, {"$set": {"key": marker, "applied_at": now}}, upsert=True)
        log.info("Migración de claims aplicada: %s", key)

    from app.services.catalogs import ensure_seed_media
    from app.services.rules import ensure_initial_rule_set
    await ensure_initial_rule_set()
    await ensure_seed_media()

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
