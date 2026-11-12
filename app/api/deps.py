"""Dependencias comunes: errores uniformes, metadatos de la petición, usuario autenticado y claims."""
import uuid
from dataclasses import dataclass, field

import jwt
from fastapi import Depends, Request

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.tokens import decode_access_token
from app.services import sessions, users


class ApiError(Exception):
    """Error con el formato que ya consume el frontend: {"error": "...", "code": "..."}."""

    def __init__(self, status: int, error: str, code: str | None = None, headers: dict | None = None):
        self.status = status
        self.error = error
        self.code = code
        self.headers = headers or {}


@dataclass
class RequestMeta:
    ip_address: str
    request_id: str
    user_agent: str


def request_meta(request: Request) -> RequestMeta:
    ip = None
    if get_settings().trust_proxy_headers:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            ip = fwd.split(",")[0].strip()
    if not ip:
        ip = request.client.host if request.client else "desconocida"
    return RequestMeta(
        ip_address=ip,
        request_id=request.headers.get("x-request-id") or f"req_{uuid.uuid4()}",
        user_agent=request.headers.get("user-agent", ""),
    )


@dataclass
class AuthContext:
    user: dict
    claims: list[str]
    session_id: str
    meta: RequestMeta
    must_change_password: bool = False
    extra: dict = field(default_factory=dict)

    def actor(self) -> dict:
        return {
            "user_id": self.user["id"],
            "user_email": self.user["email"],
            "user_roles": self.user["roles"],
            "ip_address": self.meta.ip_address,
            "request_id": self.meta.request_id,
        }


async def _authenticate(request: Request, meta: RequestMeta) -> AuthContext:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or not header[7:].strip():
        raise ApiError(401, "Sesión requerida", "UNAUTHENTICATED")
    try:
        payload = decode_access_token(header[7:].strip())
    except jwt.PyJWTError:
        raise ApiError(401, "Sesión inválida o expirada", "UNAUTHENTICATED")

    user = await users.find_by_id(payload["sub"])
    if not user or user.get("status") != "active" or not await sessions.is_session_active(payload["sid"], payload["sub"]):
        raise ApiError(401, "Sesión inválida o expirada", "UNAUTHENTICATED")
    claims = await users.resolve_claims(user["roles"])
    return AuthContext(user=user, claims=claims, session_id=payload["sid"], meta=meta,
                       must_change_password=bool(user.get("must_change_password")))


def require(*required_claims: str, allow_password_change_pending: bool = False):
    """Dependencia: exige sesión válida y TODOS los claims indicados. Las denegaciones se auditan."""

    async def dependency(request: Request, meta: RequestMeta = Depends(request_meta)) -> AuthContext:
        ctx = await _authenticate(request, meta)
        if ctx.must_change_password and not allow_password_change_pending:
            raise ApiError(403, "Debe cambiar su contraseña antes de continuar", "PASSWORD_CHANGE_REQUIRED")
        missing = [c for c in required_claims if c not in ctx.claims]
        if missing:
            await write_audit(
                **ctx.actor(), action="ACCESS_DENIED", entity_type="ENDPOINT",
                entity_id=f"{request.method} {request.url.path}", status="DENIED",
                error_message=f"Claims requeridos: {', '.join(missing)}",
            )
            raise ApiError(403, f"Acceso denegado: se requiere el permiso [{', '.join(missing)}]", "FORBIDDEN")
        return ctx

    return dependency
