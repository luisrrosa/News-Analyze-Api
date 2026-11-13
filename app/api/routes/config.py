"""Parámetros de integraciones (SMTP y SFTP). Antes vivía en Next.js; ahora FastAPI es la única autoridad.

Solo se guardan campos no secretos: las credenciales reales irán a Azure Key Vault (Fase 6), nunca a la BD.
"""
from fastapi import APIRouter, Depends, Request

from app.api.deps import ApiError, AuthContext, require
from app.core.audit import write_audit
from app.core.db import get_db, iso_now

router = APIRouter(prefix="/config", tags=["config"])

DEFAULTS = {
    "smtp": {"host": "", "port": 587, "user": "", "password_masked": "", "encryption": "STARTTLS",
             "from_name": "NEWS ANALYZE", "from_email": "", "auth_required": True, "enabled": False},
    "ftp": {"protocol": "SFTP", "host": "", "port": 22, "user": "", "auth_type": "private_key",
            "password_or_key_masked": "", "remote_path": "", "poll_interval_minutes": 15, "enabled": False},
}
FIELDS = {
    "smtp": {"host": str, "port": int, "user": str, "encryption": ("TLS", "SSL", "STARTTLS"), "from_name": str,
             "from_email": str, "auth_required": bool, "enabled": bool},
    "ftp": {"protocol": ("SFTP", "FTPS", "FTP"), "host": str, "port": int, "user": str,
            "auth_type": ("password", "private_key"), "remote_path": str, "poll_interval_minutes": int, "enabled": bool},
}


async def load() -> dict:
    cfg = await get_db()["system_configs"].find_one({"key": "integrations"}, {"_id": 0}) or {}
    return {k: {**DEFAULTS[k], **(cfg.get(k) or {})} for k in DEFAULTS}


def _clean(section: str, data: dict) -> dict:
    out: dict = {}
    for field, kind in FIELDS[section].items():
        if field not in data:
            continue
        v = data[field]
        if isinstance(kind, tuple):
            if v not in kind:
                raise ApiError(400, f"Valor inválido para {field}", "CONFIG_INVALID")
        elif kind is int:
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ApiError(400, f"{field} debe ser un número", "CONFIG_INVALID")
            if field == "port" and not 1 <= v <= 65535:
                raise ApiError(400, "Puerto inválido", "CONFIG_INVALID")
            if field == "poll_interval_minutes" and not 1 <= v <= 1440:
                raise ApiError(400, "Intervalo inválido (1–1440 minutos)", "CONFIG_INVALID")
        elif kind is bool:
            if not isinstance(v, bool):
                raise ApiError(400, f"{field} debe ser verdadero o falso", "CONFIG_INVALID")
        else:
            if not isinstance(v, str) or len(v) > 300:
                raise ApiError(400, f"{field} inválido", "CONFIG_INVALID")
            v = v.strip()
        out[field] = v
    return out


@router.get("")
async def get_config(ctx: AuthContext = Depends(require("config:read"))):
    return await load()


@router.post("")
async def post_config(request: Request, ctx: AuthContext = Depends(require("config:manage"))):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    action, section, data = body.get("action"), body.get("section"), body.get("data") or {}

    if action == "test_ftp":
        # La conexión SFTP real llegará con el agente de ingesta remota. No se simula un éxito.
        return {"success": False, "latency_ms": 0,
                "message": "Prueba de conexión SFTP no implementada todavía: llegará con el agente de ingesta remota."}

    if action == "update" and section in FIELDS and isinstance(data, dict):
        current = await load()
        changes = _clean(section, data)
        updated = {**current, section: {**current[section], **changes}}
        await get_db()["system_configs"].update_one(
            {"key": "integrations"},
            {"$set": {"key": "integrations", "smtp": updated["smtp"], "ftp": updated["ftp"],
                      "updated_by": ctx.user["id"], "updated_at": iso_now()}},
            upsert=True)
        await write_audit(**ctx.actor(), action=f"CONFIG_{section.upper()}_UPDATED", entity_type="CONFIG",
                          entity_id=section.upper(), status="SUCCESS",
                          details={"before": current[section], "after": updated[section]})
        return {"success": True, "message": f"Parámetros de {section.upper()} guardados.", "config": updated}

    raise ApiError(400, "Acción no reconocida", "CONFIG_ACTION")
