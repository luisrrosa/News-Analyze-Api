"""Endurecimiento de seguridad (revisión 2026-11): límites de cuerpo, bloqueo atómico, sesiones, escalada de
privilegios, permisos por acción, inyección de operadores y CSV."""
import asyncio

import pytest

from app.core.db import get_db
from tests.conftest import admin_session, login, new_ip, strong_password, user_session

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


@pytest.fixture
async def team(client):
    admin = await admin_session(client)
    return {"admin": admin, **{r: await user_session(client, admin, r) for r in ("editor", "consulta")}}


async def test_body_limits_before_auth(client):
    # Sin sesión: un cuerpo declarado enorme se rechaza antes de leerlo (413), y sin cuerpo grande, 401.
    r = await client.post("/api/v1/documents/upload", headers={"content-length": str(500 * 1024 * 1024),
                                                               "content-type": "multipart/form-data; boundary=x"}, content=b"")
    assert r.status_code == 413
    r = await client.post("/api/v1/documents/upload", files={"file": ("a.pdf", b"%PDF-1.4 x", "application/pdf")})
    assert r.status_code == 401
    # JSON de más de 1 MB en cualquier otra ruta
    r = await client.post("/api/v1/auth/login", content=b"{" + b" " * (2 * 1024 * 1024) + b"}",
                          headers={"content-type": "application/json"})
    assert r.status_code == 413


async def test_parallel_failed_logins_still_lock(team, client):
    email = team["consulta"].user["email"]
    ip_base = new_ip()
    results = await asyncio.gather(*[login(client, email, "incorrecta-" + str(i), ip=f"{ip_base}{i}") for i in range(8)])
    assert all(r.status_code in (401, 429) for r in results)
    u = await get_db()["users"].find_one({"email": email})
    assert u.get("locked_until"), "con 8 intentos en paralelo la cuenta debe quedar bloqueada"
    # Bloqueada + clave incorrecta: respuesta genérica (no revela que la cuenta existe).
    r = await login(client, email, "otra-incorrecta", new_ip())
    assert r.status_code == 401 and r.json()["code"] == "INVALID_CREDENTIALS"
    await get_db()["users"].update_one({"email": email}, {"$set": {"locked_until": None, "failed_attempts": 0}})


async def test_change_password_revokes_other_sessions(team, client):
    s = team["editor"]
    r = await login(client, s.user["email"], s.password, s.ip)
    other_token = r.json()["access_token"]
    new = strong_password()
    assert (await s.post("/api/v1/auth/change-password", json={"current_password": s.password, "new_password": new})).status_code == 200
    s.password = new
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {other_token}"})).status_code == 401
    assert (await s.get("/api/v1/auth/me")).status_code == 200   # la sesión actual sigue


async def test_no_privilege_escalation(team):
    admin, editor = team["admin"], team["editor"]
    claims = (await admin.get("/api/v1/roles")).json()["role_claims"]["editor"]
    # Un no-admin con users:read + users:assign_roles no puede darse el rol admin.
    granted = claims + ["users:read", "users:assign_roles", "roles:manage"]
    assert (await admin.post("/api/v1/roles", json={"role": "editor", "claims": granted})).status_code == 200
    try:
        r = await editor.put(f"/api/v1/users/{editor.user['id']}", json={"roles": ["editor", "admin"]})
        assert r.status_code == 403
        # ni concederse permisos de seguridad desde la matriz
        r = await editor.post("/api/v1/roles", json={"role": "consulta", "claims": ["dashboard:read", "users:create"]})
        assert r.status_code == 403
        r = await editor.post("/api/v1/roles", json={"role": "admin", "claims": []})
        assert r.status_code == 403
    finally:
        await admin.post("/api/v1/roles", json={"role": "editor", "claims": claims})


async def test_field_level_user_claims(team):
    admin, consulta = team["admin"], team["consulta"]
    claims = (await admin.get("/api/v1/roles")).json()["role_claims"]["consulta"]
    assert (await admin.post("/api/v1/roles", json={"role": "consulta", "claims": claims + ["users:read", "users:update"]})).status_code == 200
    try:
        target = team["editor"].user["id"]
        assert (await consulta.put(f"/api/v1/users/{target}", json={"name": "Nombre nuevo"})).status_code == 200
        assert (await consulta.put(f"/api/v1/users/{target}", json={"status": "inactive"})).status_code == 403
        assert (await consulta.post(f"/api/v1/users/{target}/reset-password")).status_code == 403
    finally:
        await admin.post("/api/v1/roles", json={"role": "consulta", "claims": claims})


async def test_config_claims_per_section(team):
    admin = team["admin"]
    assert (await admin.post("/api/v1/config", json={"action": "test_ftp"})).status_code == 200
    claims = (await admin.get("/api/v1/roles")).json()["role_claims"]["editor"]
    await admin.post("/api/v1/roles", json={"role": "editor", "claims": claims + ["config:read"]})
    try:
        e = team["editor"]
        assert (await e.get("/api/v1/config")).status_code == 200
        assert (await e.post("/api/v1/config", json={"action": "update", "section": "smtp", "data": {"host": "x"}})).status_code == 403
        assert (await e.post("/api/v1/config", json={"action": "test_ftp"})).status_code == 403
    finally:
        await admin.post("/api/v1/roles", json={"role": "editor", "claims": claims})


async def test_roles_endpoint_exposes_catalog(team):
    body = (await team["admin"].get("/api/v1/roles")).json()
    ids = {c["id"] for c in body["all_claims"]}
    assert {"review:edit_text", "reports:export_csv", "audit:verify", "users:reset_password"} <= ids
    assert "users:manage" not in ids and "news:edit" not in ids
    assert body["can_grant_privileged"] is True and "roles:manage" in body["admin_locked_claims"]


def test_csv_formula_cells_are_neutralized():
    from app.api.routes.news import _csv_cell
    assert _csv_cell("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
    assert _csv_cell("+1") == "'+1" and _csv_cell("@cmd") == "'@cmd" and _csv_cell("Normal") == "Normal"
    assert _csv_cell(5) == 5


async def test_body_limit_streamed_without_length(app_ctx):
    """Cuerpo por partes sin Content-Length: se corta al pasar el límite."""
    from app.core.body_limit import BodyLimitMiddleware, DEFAULT_LIMIT
    sent = []

    async def downstream(scope, receive, send):
        while True:
            m = await receive()
            if not m.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    chunks = [{"type": "http.request", "body": b"x" * (512 * 1024), "more_body": True} for _ in range(4)]
    chunks.append({"type": "http.request", "body": b"", "more_body": False})

    async def receive():
        return chunks.pop(0)

    async def send(m):
        sent.append(m)

    await BodyLimitMiddleware(downstream)({"type": "http", "method": "POST", "path": "/api/v1/x", "headers": []}, receive, send)
    assert sent[0]["status"] == 413 and DEFAULT_LIMIT < 2 * 1024 * 1024
