"""Pruebas contra un MongoDB real (local o contenedor de CI). Cada ejecución usa una BD única que se borra al final."""
import os
import secrets
import uuid

import pytest
import pytest_asyncio

import tempfile

_TMP = tempfile.mkdtemp(prefix="news_analyze_test_")
ADMIN_EMAIL = "admin.test@local.test"
ADMIN_PASSWORD = "Boot" + secrets.token_hex(6) + "X1"
TEST_DB = f"news_analyze_pytest_{uuid.uuid4().hex[:10]}"

os.environ.update({
    "APP_ENV": "test",
    "MONGODB_URI": os.environ.get("TEST_MONGODB_URI", "mongodb://localhost:27017"),
    "MONGODB_DB_NAME": TEST_DB,
    "MONGODB_DNS_SERVERS": "",
    "JWT_SECRET_KEY": secrets.token_urlsafe(48),
    "HMAC_AUDIT_SECRET": secrets.token_urlsafe(48),
    "BOOTSTRAP_ADMIN_EMAIL": ADMIN_EMAIL,
    "BOOTSTRAP_ADMIN_PASSWORD": ADMIN_PASSWORD,
    "COOKIE_SECURE": "false",  # httpx de prueba usa http://
    "REDIS_URL": "",
    "REFRESH_GRACE_SECONDS": "45",
    "CELERY_ALWAYS_EAGER": "true",       # el trabajo se ejecuta en el acto, sin broker
    "STORAGE_ROOT": os.path.join(_TMP, "storage"),
    "INGEST_FOLDER": os.path.join(_TMP, "inbox"),
    "INGEST_STABLE_SECONDS": "1",
})

import httpx  # noqa: E402

from app.core import db  # noqa: E402
from app.core.ratelimit import reset_memory  # noqa: E402
from app.main import app  # noqa: E402

_ip_counter = 0


def new_ip() -> str:
    """IP distinta por escenario para no mezclar el límite de intentos de login."""
    global _ip_counter
    _ip_counter += 1
    return f"10.{_ip_counter // 250}.{_ip_counter % 250}.1"


def strong_password() -> str:
    return "Tst" + secrets.token_hex(5) + "A9x"


@pytest_asyncio.fixture(scope="session")
async def app_ctx():
    async with app.router.lifespan_context(app):
        yield app
        await db.get_db().client.drop_database(TEST_DB)
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest_asyncio.fixture
async def client(app_ctx):
    reset_memory()
    transport = httpx.ASGITransport(app=app_ctx)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class Session:
    def __init__(self, client: httpx.AsyncClient, data: dict, password: str, ip: str):
        self.client = client
        self.token = data["access_token"]
        self.user = data["user"]
        self.password = password
        self.ip = ip

    def headers(self, extra: dict | None = None) -> dict:
        return {"Authorization": f"Bearer {self.token}", "x-forwarded-for": self.ip, **(extra or {})}

    async def get(self, path: str, **kw):
        return await self.client.get(path, headers=self.headers(kw.pop("headers", None)), **kw)

    async def post(self, path: str, **kw):
        return await self.client.post(path, headers=self.headers(kw.pop("headers", None)), **kw)

    async def put(self, path: str, **kw):
        return await self.client.put(path, headers=self.headers(kw.pop("headers", None)), **kw)


async def login(client: httpx.AsyncClient, email: str, password: str, ip: str | None = None) -> httpx.Response:
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password},
                             headers={"x-forwarded-for": ip or new_ip()})


_admin_password = {"value": ADMIN_PASSWORD}


async def admin_session(client: httpx.AsyncClient) -> Session:
    """Admin listo para operar: si aún tiene la clave inicial, la cambia."""
    ip = new_ip()
    r = await login(client, ADMIN_EMAIL, _admin_password["value"], ip)
    assert r.status_code == 200, r.text
    s = Session(client, r.json(), _admin_password["value"], ip)
    if s.user["must_change_password"]:
        new = strong_password()
        r = await s.post("/api/v1/auth/change-password", json={"current_password": s.password, "new_password": new})
        assert r.status_code == 200, r.text
        _admin_password["value"] = new
        s.password = new
    return s


async def user_session(client: httpx.AsyncClient, admin: Session, role: str) -> Session:
    """Crea un usuario del rol indicado, cambia su clave inicial y devuelve su sesión."""
    email = f"{role}.{uuid.uuid4().hex[:8]}@local.test"
    initial = strong_password()
    r = await admin.post("/api/v1/users", json={"name": f"Prueba {role}", "email": email, "roles": [role], "password": initial})
    assert r.status_code == 201, r.text
    ip = new_ip()
    r = await login(client, email, initial, ip)
    first = Session(client, r.json(), initial, ip)
    final = strong_password()
    r = await first.post("/api/v1/auth/change-password", json={"current_password": initial, "new_password": final})
    assert r.status_code == 200, r.text
    r = await login(client, email, final, ip)
    assert r.status_code == 200, r.text
    s = Session(client, r.json(), final, ip)
    s.user["email"] = email
    return s
