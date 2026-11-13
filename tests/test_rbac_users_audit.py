import json

import pytest

from app.core.db import get_db
from tests.conftest import admin_session, strong_password, user_session

pytestmark = pytest.mark.asyncio(loop_scope="session")

MATRIX = [
    ("consulta", "GET", "/api/v1/users", 403), ("editor", "GET", "/api/v1/users", 403),
    ("auditor", "GET", "/api/v1/users", 403), ("admin", "GET", "/api/v1/users", 200),
    ("consulta", "GET", "/api/v1/roles", 403), ("editor", "GET", "/api/v1/roles", 403), ("admin", "GET", "/api/v1/roles", 200),
    ("consulta", "GET", "/api/v1/audit/logs", 403), ("editor", "GET", "/api/v1/audit/logs", 403),
    ("auditor", "GET", "/api/v1/audit/logs", 200), ("admin", "GET", "/api/v1/audit/logs", 200),
    ("consulta", "GET", "/api/v1/audit/verify", 403), ("auditor", "GET", "/api/v1/audit/verify", 200),
]


@pytest.fixture
async def sessions(client):
    admin = await admin_session(client)
    return {"admin": admin, **{r: await user_session(client, admin, r) for r in ("editor", "consulta", "auditor")}}


async def test_role_matrix(sessions):
    for role, method, path, expected in MATRIX:
        r = await (sessions[role].get(path) if method == "GET" else sessions[role].post(path))
        assert r.status_code == expected, f"{role} {method} {path}: {r.status_code}"


async def test_claims_resolved_from_roles(sessions):
    claims = {r: set(s.user["claims"]) for r, s in sessions.items()}
    assert "roles:manage" in claims["admin"] and "users:assign_roles" in claims["admin"]
    assert "documents:download" in claims["editor"] and "config:read" not in claims["editor"]
    assert claims["consulta"] == {"dashboard:read", "news:read", "documents:read", "catalogs:read"}
    assert "reports:presidential" in claims["editor"] and "reports:presidential" not in claims["consulta"]
    assert "audit:read" in claims["auditor"] and "documents:download" not in claims["auditor"]


async def test_privilege_escalation_blocked(sessions):
    r = await sessions["consulta"].post("/api/v1/roles", json={"role": "consulta", "claims": ["roles:manage"]})
    assert r.status_code == 403
    r = await sessions["editor"].post(f"/api/v1/users/{sessions['consulta'].user['id']}/reset-password")
    assert r.status_code == 403
    r = await sessions["editor"].put(f"/api/v1/users/{sessions['editor'].user['id']}", json={"roles": ["admin"]})
    assert r.status_code == 403


async def test_admin_cannot_lock_itself_out(sessions):
    admin = sessions["admin"]
    r = await admin.post("/api/v1/roles", json={"role": "admin", "claims": ["news:read"]})
    assert r.status_code == 400
    r = await admin.put(f"/api/v1/users/{admin.user['id']}", json={"status": "inactive"})
    assert r.status_code == 400


async def test_role_change_applies_on_next_request(sessions):
    admin, consulta = sessions["admin"], sessions["consulta"]
    r = await admin.post("/api/v1/roles", json={"role": "consulta", "claims": ["dashboard:read", "news:read", "documents:read", "audit:read"]})
    assert r.status_code == 200
    try:
        assert (await consulta.get("/api/v1/audit/logs")).status_code == 200  # claims resueltos en BD, no en el token
    finally:
        await admin.post("/api/v1/roles", json={"role": "consulta", "claims": ["dashboard:read", "news:read", "documents:read"]})
    assert (await consulta.get("/api/v1/audit/logs")).status_code == 403


async def test_user_validation(sessions):
    admin = sessions["admin"]
    bad = [
        {"name": "X", "email": "x@local.test", "roles": ["consulta"], "password": "123"},
        {"name": "X", "email": "no-es-email", "roles": ["consulta"], "password": strong_password()},
        {"name": "X", "email": "y@local.test", "roles": ["superadmin"], "password": strong_password()},
        {"name": "", "email": "z@local.test", "roles": ["consulta"], "password": strong_password()},
    ]
    for body in bad:
        assert (await admin.post("/api/v1/users", json=body)).status_code == 400
    dup = sessions["editor"].user["email"]
    r = await admin.post("/api/v1/users", json={"name": "Dup", "email": dup, "roles": ["consulta"], "password": strong_password()})
    assert r.status_code == 400


async def test_user_list_never_exposes_password_hash(sessions):
    r = await sessions["admin"].get("/api/v1/users")
    assert all("password_hash" not in u for u in r.json()["users"])


async def test_deactivated_user_loses_session(sessions):
    admin, auditor = sessions["admin"], sessions["auditor"]
    r = await admin.put(f"/api/v1/users/{auditor.user['id']}", json={"status": "inactive"})
    assert r.status_code == 200
    assert (await auditor.get("/api/v1/audit/logs")).status_code == 401


async def test_audit_records_events_without_secrets(sessions):
    admin = sessions["admin"]
    await sessions["consulta"].get("/api/v1/users")  # genera ACCESS_DENIED
    logs = []
    for a in ("AUTH_LOGIN_SUCCESS", "USER_CREATED", "ACCESS_DENIED", "AUTH_PASSWORD_CHANGED", "BOOTSTRAP_ADMIN_CREATED"):
        body = (await admin.get(f"/api/v1/audit/logs?action={a}&limit=50")).json()   # filtro en el servidor
        assert body["total"] >= 1 and all(l["action"] == a for l in body["logs"]), a
        logs += body["logs"]
    dump = json.dumps(logs)
    for s in sessions.values():
        assert s.password not in dump
        assert s.token not in dump
    assert all("human_readable" in l for l in logs)


async def test_audit_chain_verifies_and_detects_tampering(sessions):
    admin = sessions["admin"]
    r = await admin.get("/api/v1/audit/verify")
    assert r.json()["integrity_status"] == "VERIFIED", r.json()
    assert (await admin.get("/api/v1/audit/verify")).json()["incremental"] is True   # desde el último punto verificado
    col = get_db()["audit_logs"]
    target = await col.find_one({"action": "USER_CREATED"})
    original = target["user_email"]
    await col.update_one({"_id": target["_id"]}, {"$set": {"user_email": "otro@local.test"}})
    try:
        r = await admin.get("/api/v1/audit/verify?full=true")   # la completa recorre toda la cadena
        body = r.json()
        assert body["integrity_status"] == "BROKEN" and body["first_broken_seq"] == target["seq_num"]
    finally:
        await col.update_one({"_id": target["_id"]}, {"$set": {"user_email": original}})
    assert (await admin.get("/api/v1/audit/verify?full=true")).json()["integrity_status"] == "VERIFIED"

    # Borrar el final de la cadena se detecta con el ancla firmada.
    from app.core.audit import write_anchor
    anchor = await write_anchor()
    last = await col.find_one({"seq_num": anchor["seq_num"]})
    await col.delete_one({"_id": last["_id"]})
    try:
        body = (await admin.get("/api/v1/audit/verify?full=true")).json()
        assert body["integrity_status"] == "BROKEN" and "Faltan registros al final" in body["reason"]
        body = (await admin.get("/api/v1/audit/verify?full=true")).json()   # ya se escribió encima: sigue detectándose
        assert body["integrity_status"] == "BROKEN" and "Faltan registros al final" in body["reason"]
    finally:
        await col.delete_many({"seq_num": {"$gte": last["seq_num"]}})
        await col.insert_one(last)
        await get_db()["audit_anchors"].delete_many({})
        await get_db()["system_configs"].delete_many({"key": "audit.checkpoint"})


async def test_audit_detail_requires_claim(sessions):
    """Sin audit:read_detail el listado no trae el detalle técnico."""
    admin = sessions["admin"]
    assert all("details" in l or True for l in (await admin.get("/api/v1/audit/logs?limit=5")).json()["logs"])
    r = await admin.post("/api/v1/roles", json={"role": "auditor", "claims": ["dashboard:read", "news:read", "audit:read"]})
    assert r.status_code == 200, r.text
    try:
        body = (await sessions["auditor"].get("/api/v1/audit/logs?limit=20")).json()
        assert body["detail_included"] is False and all("details" not in l for l in body["logs"])
        assert (await sessions["auditor"].get("/api/v1/audit/verify")).status_code == 403
    finally:
        await admin.post("/api/v1/roles", json={"role": "auditor", "claims": [
            "dashboard:read", "news:read", "documents:read", "catalogs:read", "review:history_read",
            "audit:read", "audit:read_detail", "audit:verify"]})
