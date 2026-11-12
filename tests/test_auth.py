import asyncio
from contextlib import asynccontextmanager
import base64
import hashlib
import os
from datetime import timedelta

import httpx
import pytest

from app.core.db import get_db, utcnow
from app.core.tokens import sha256
from tests.conftest import (ADMIN_EMAIL, ADMIN_PASSWORD, Session, admin_session, login, new_ip, strong_password,
                            user_session)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_bootstrap_admin_must_change_password(client):
    r = await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    if r.status_code == 401:  # otra prueba ya cambió la clave inicial
        return
    assert r.status_code == 200
    s = Session(client, r.json(), ADMIN_PASSWORD, new_ip())
    assert s.user["must_change_password"] is True
    r = await s.get("/api/v1/users")
    assert r.status_code == 403 and r.json()["code"] == "PASSWORD_CHANGE_REQUIRED"
    r = await s.post("/api/v1/auth/change-password", json={"current_password": ADMIN_PASSWORD, "new_password": "corta"})
    assert r.status_code == 400


async def test_login_sets_httponly_refresh_and_csrf_cookies(client):
    admin = await admin_session(client)
    r = await login(client, ADMIN_EMAIL, admin.password)
    cookies = r.headers.get_list("set-cookie")
    refresh = next(c for c in cookies if c.startswith("na_refresh="))
    csrf = next(c for c in cookies if c.startswith("na_csrf="))
    assert "HttpOnly" in refresh and "Path=/api/v1/auth" in refresh and "SameSite=strict" in refresh
    assert "HttpOnly" not in csrf
    assert "access_token" in r.json()


async def test_invalid_credentials_same_message(client):
    r1 = await login(client, "nadie@local.test", "Cualquiera123")
    admin = await admin_session(client)
    r2 = await login(client, ADMIN_EMAIL, "Incorrecta123")
    assert r1.status_code == r2.status_code == 401
    assert r1.json() == r2.json()


async def test_scrypt_hash_is_migrated_to_argon2id(client):
    # Usuario creado por la app Next.js (scrypt$N$r$p$salt$hash)
    password = strong_password()
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=64)
    stored = f"scrypt$16384$8$1${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"
    await get_db()["users"].insert_one({
        "id": "usr_legacy_scrypt", "email": "legacy@local.test", "name": "Legado", "roles": ["consulta"],
        "status": "active", "created_at": "2026-11-12T00:00:00.000Z", "password_hash": stored,
        "must_change_password": False, "failed_attempts": 0, "locked_until": None,
    })
    r = await login(client, "legacy@local.test", password)
    assert r.status_code == 200, r.text
    doc = await get_db()["users"].find_one({"id": "usr_legacy_scrypt"})
    assert doc["password_hash"].startswith("$argon2id$")
    r = await login(client, "legacy@local.test", password)
    assert r.status_code == 200


async def test_account_lock_after_five_failures(client):
    admin = await admin_session(client)
    u = await user_session(client, admin, "consulta")
    for _ in range(5):
        await login(client, u.user["email"], "Incorrecta123", new_ip())
    r = await login(client, u.user["email"], u.password, new_ip())
    assert r.status_code == 403 and r.json()["code"] == "ACCOUNT_LOCKED"


async def test_login_rate_limit_per_ip(client):
    ip = new_ip()
    statuses = [(await login(client, "nadie@local.test", "x", ip)).status_code for _ in range(11)]
    assert statuses[-1] == 429


async def test_tampered_and_missing_tokens_rejected(client):
    admin = await admin_session(client)
    assert (await client.get("/api/v1/users")).status_code == 401
    r = await client.get("/api/v1/users", headers={"Authorization": f"Bearer {admin.token[:-3]}abc"})
    assert r.status_code == 401


async def test_logout_revokes_access_token(client):
    admin = await admin_session(client)
    u = await user_session(client, admin, "auditor")
    assert (await u.post("/api/v1/auth/logout")).status_code == 200
    assert (await u.get("/api/v1/audit/logs")).status_code == 401


# ---------------- Refresh rotativo ----------------

@asynccontextmanager
async def _fresh_client_login(app_ctx, email, password):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_ctx), base_url="http://test") as c:
        r = await login(c, email, password)
        assert r.status_code == 200, r.text
        yield c, r.json()


def _csrf(c: httpx.AsyncClient) -> dict:
    return {"x-csrf-token": c.cookies.get("na_csrf")}


async def test_refresh_requires_csrf(client, app_ctx):
    admin = await admin_session(client)
    async with _fresh_client_login(app_ctx, ADMIN_EMAIL, admin.password) as (c, _):
        r = await c.post("/api/v1/auth/refresh")
        assert r.status_code == 403 and r.json()["code"] == "CSRF_FAILED"
        r = await c.post("/api/v1/auth/refresh", headers={"x-csrf-token": "otro"})
        assert r.status_code == 403


async def test_refresh_rotates_and_issues_new_cookie(client, app_ctx):
    admin = await admin_session(client)
    async with _fresh_client_login(app_ctx, ADMIN_EMAIL, admin.password) as (c, data):
        old = c.cookies.get("na_refresh")
        r = await c.post("/api/v1/auth/refresh", headers=_csrf(c))
        assert r.status_code == 200, r.text
        assert c.cookies.get("na_refresh") != old
        assert r.json()["user"]["session_id"] == data["user"]["session_id"]
        r = await c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
        assert r.status_code == 200


async def test_ten_concurrent_refreshes_within_grace_keep_session(client, app_ctx):
    admin = await admin_session(client)
    async with _fresh_client_login(app_ctx, ADMIN_EMAIL, admin.password) as (c, _):
        token = c.cookies.get("na_refresh")
        csrf = c.cookies.get("na_csrf")
        cookies = {"na_refresh": token, "na_csrf": csrf}

        async def one():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_ctx), base_url="http://test",
                                         cookies=cookies) as cc:
                return (await cc.post("/api/v1/auth/refresh", headers={"x-csrf-token": csrf})).status_code

        statuses = await asyncio.gather(*[one() for _ in range(10)])
        assert statuses == [200] * 10
        session = await get_db()["user_sessions"].find_one({"consumed_tokens.token_hash": sha256(token)})
        assert session["revoked_at"] is None


async def test_reuse_after_grace_revokes_family_without_locking_account(client, app_ctx):
    admin = await admin_session(client)
    u = await user_session(client, admin, "consulta")
    async with _fresh_client_login(app_ctx, u.user["email"], u.password) as (c, data):
        old = c.cookies.get("na_refresh")
        csrf = c.cookies.get("na_csrf")
        r = await c.post("/api/v1/auth/refresh", headers={"x-csrf-token": csrf})
        new_access = r.json()["access_token"]
        # Simula que el token viejo se consumió hace más de la ventana de gracia.
        await get_db()["user_sessions"].update_one(
            {"consumed_tokens.token_hash": sha256(old)},
            {"$set": {"consumed_tokens.$.consumed_at": utcnow() - timedelta(seconds=120)}},
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_ctx), base_url="http://test",
                                     cookies={"na_refresh": old, "na_csrf": csrf}) as attacker:
            r = await attacker.post("/api/v1/auth/refresh", headers={"x-csrf-token": csrf})
            assert r.status_code == 401
        # Toda la familia queda revocada: el access token legítimo deja de valer.
        r = await c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {new_access}"})
        assert r.status_code == 401
        # La cuenta no se bloquea: el usuario puede volver a entrar.
        r = await login(client, u.user["email"], u.password)
        assert r.status_code == 200


async def test_password_reset_revokes_sessions(client):
    admin = await admin_session(client)
    u = await user_session(client, admin, "consulta")
    r = await admin.post(f"/api/v1/users/{u.user['id']}/reset-password")
    assert r.status_code == 200 and isinstance(r.json()["temp_password"], str)
    assert r.headers["cache-control"] == "no-store"
    assert (await u.get("/api/v1/auth/me")).status_code == 401
    r = await login(client, u.user["email"], r.json()["temp_password"])
    assert r.status_code == 200 and r.json()["user"]["must_change_password"] is True
