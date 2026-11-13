"""Configuración de integraciones (SMTP/SFTP) servida por FastAPI."""
import pytest

from tests.test_documents import roles  # noqa: F401  (fixture)

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


async def test_config_permissions(roles):
    for role in ("consulta", "editor"):
        assert (await roles[role].get("/api/v1/config")).status_code == 403
    assert (await roles["auditor"].post("/api/v1/config", json={"action": "update", "section": "smtp", "data": {}})).status_code == 403


async def test_config_update_validates_and_audits(roles):
    admin = roles["admin"]
    cfg = (await admin.get("/api/v1/config")).json()
    assert cfg["smtp"]["port"] == 587 and cfg["ftp"]["protocol"] == "SFTP"

    bad = await admin.post("/api/v1/config", json={"action": "update", "section": "smtp", "data": {"port": 70000}})
    assert bad.status_code == 400
    bad = await admin.post("/api/v1/config", json={"action": "update", "section": "ftp", "data": {"protocol": "HTTP"}})
    assert bad.status_code == 400

    r = await admin.post("/api/v1/config", json={"action": "update", "section": "smtp",
                                                 "data": {"host": " smtp.ejemplo.do ", "port": "465", "password": "no-se-guarda"}})
    assert r.status_code == 200
    smtp = r.json()["config"]["smtp"]
    assert smtp["host"] == "smtp.ejemplo.do" and smtp["port"] == 465 and "password" not in smtp
    assert (await admin.get("/api/v1/config")).json()["smtp"]["host"] == "smtp.ejemplo.do"

    logs = (await admin.get("/api/v1/audit/logs")).json()
    assert any(l["action"] == "CONFIG_SMTP_UPDATED" for l in logs["logs"])

    t = await admin.post("/api/v1/config", json={"action": "test_ftp"})
    assert t.status_code == 200 and t.json()["success"] is False
