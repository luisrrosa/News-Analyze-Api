import hmac
import secrets

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.api.deps import ApiError, AuthContext, RequestMeta, request_meta, require
from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.passwords import hash_password, password_policy_error, verify_password
from app.core.ratelimit import hit
from app.core.tokens import issue_access_token
from app.services import sessions, users

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "na_refresh"
CSRF_COOKIE = "na_csrf"
CSRF_HEADER = "x-csrf-token"


def _set_auth_cookies(response: Response, refresh_token: str) -> None:
    s = get_settings()
    max_age = s.refresh_token_expire_days * 86400
    # Refresh: HttpOnly, solo viaja a /api/v1/auth. CSRF: legible por JS para el doble envío.
    response.set_cookie(REFRESH_COOKIE, refresh_token, max_age=max_age, httponly=True,
                        secure=s.cookie_secure, samesite="strict", path="/api/v1/auth")
    response.set_cookie(CSRF_COOKIE, secrets.token_urlsafe(24), max_age=max_age, httponly=False,
                        secure=s.cookie_secure, samesite="strict", path="/")


def _clear_auth_cookies(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth", secure=s.cookie_secure, samesite="strict")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=s.cookie_secure, samesite="strict")


def _check_csrf(request: Request) -> None:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get(CSRF_HEADER, "")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise ApiError(403, "Token CSRF inválido", "CSRF_FAILED")


async def _session_payload(user: dict, session_id: str) -> dict:
    s = get_settings()
    claims = await users.resolve_claims(user["roles"])
    return {
        "access_token": issue_access_token(user["id"], session_id),
        "token_type": "bearer",
        "expires_in": s.access_token_expire_minutes * 60,
        "user": {
            "id": user["id"], "email": user["email"], "name": user["name"], "roles": user["roles"],
            "claims": claims, "session_id": session_id,
            "must_change_password": bool(user.get("must_change_password")),
        },
    }


@router.post("/login")
async def login(request: Request, response: Response, meta: RequestMeta = Depends(request_meta)):
    s = get_settings()
    allowed, retry = await hit(f"login:{meta.ip_address}", s.login_rate_limit, s.login_rate_window_seconds)
    if not allowed:
        raise ApiError(429, "Demasiados intentos. Espere antes de volver a intentar.", "RATE_LIMITED",
                       headers={"Retry-After": str(retry)})

    try:
        body = await request.json()
    except ValueError:
        raise ApiError(400, "Solicitud inválida")
    email = body.get("email") if isinstance(body, dict) else None
    password = body.get("password") if isinstance(body, dict) else None
    if not isinstance(email, str) or not isinstance(password, str) or not email.strip() or not password \
            or len(email) > 254 or len(password) > 256:
        raise ApiError(400, "Email y contraseña requeridos")
    email = email.strip().lower()

    user = await users.find_by_email(email)
    ok, needs_rehash = verify_password(password, user.get("password_hash") if user else None)
    base = {"ip_address": meta.ip_address, "request_id": meta.request_id}

    if user and users.is_locked(user):
        await write_audit(user_id=user["id"], user_email=user["email"], user_roles=user["roles"],
                          action="AUTH_LOGIN_BLOCKED", entity_type="USER", entity_id=user["id"], status="DENIED",
                          error_message="Cuenta bloqueada temporalmente por intentos fallidos", **base)
        raise ApiError(403, "Cuenta bloqueada temporalmente por intentos fallidos. Intente más tarde.", "ACCOUNT_LOCKED")

    if not user or not ok:
        locked_now = await users.register_failed_login(user) if user else False
        await write_audit(user_id=user["id"] if user else "desconocido", user_email=email,
                          user_roles=user["roles"] if user else [], action="AUTH_LOGIN_FAILED", entity_type="USER",
                          entity_id=user["id"] if user else email, status="DENIED",
                          error_message="Credenciales inválidas; cuenta bloqueada 15 min" if locked_now else "Credenciales inválidas",
                          **base)
        raise ApiError(401, "Credenciales inválidas", "INVALID_CREDENTIALS")

    if user.get("status") != "active":
        await write_audit(user_id=user["id"], user_email=user["email"], user_roles=user["roles"],
                          action="AUTH_LOGIN_BLOCKED", entity_type="USER", entity_id=user["id"], status="DENIED",
                          error_message=f"Cuenta con estado {user.get('status')}", **base)
        raise ApiError(403, "Cuenta inactiva o bloqueada. Contacte al administrador.", "ACCOUNT_INACTIVE")

    if needs_rehash:
        # Migración transparente scrypt → Argon2id (o actualización de parámetros Argon2).
        await users.set_password(user["id"], hash_password(password), bool(user.get("must_change_password")))
    await users.register_successful_login(user)
    session_id, refresh = await sessions.create_session(user["id"], meta.ip_address, meta.user_agent)
    payload = await _session_payload(user, session_id)
    _set_auth_cookies(response, refresh)
    await write_audit(user_id=user["id"], user_email=user["email"], user_roles=user["roles"],
                      action="AUTH_LOGIN_SUCCESS", entity_type="USER_SESSION", entity_id=session_id, status="SUCCESS",
                      details={"claims_count": len(payload["user"]["claims"]), "password_rehashed": needs_rehash or None},
                      **base)
    return payload


@router.post("/refresh")
async def refresh(request: Request, response: Response, meta: RequestMeta = Depends(request_meta)):
    _check_csrf(request)
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise ApiError(401, "Sesión requerida", "UNAUTHENTICATED")

    result = await sessions.rotate_refresh(token)
    if result.status in ("invalid", "reused"):
        if result.status == "reused" and result.user_id:
            u = await users.find_by_id(result.user_id)
            await write_audit(user_id=result.user_id, user_email=(u or {}).get("email", ""),
                              user_roles=(u or {}).get("roles", []), action="AUTH_REFRESH_REUSE_DETECTED",
                              entity_type="USER_SESSION", entity_id=result.session_id or "", status="DENIED",
                              ip_address=meta.ip_address, request_id=meta.request_id,
                              error_message="Reutilización de refresh token: familia revocada")
        denied = JSONResponse({"error": "Sesión inválida o expirada", "code": "UNAUTHENTICATED"}, status_code=401)
        _clear_auth_cookies(denied)
        return denied

    user = await users.find_by_id(result.user_id or "")
    if not user or user.get("status") != "active":
        await sessions.revoke_session(result.session_id or "", "user_inactive")
        raise ApiError(401, "Sesión inválida o expirada", "UNAUTHENTICATED")

    payload = await _session_payload(user, result.session_id or "")
    if result.status == "rotated" and result.refresh_token:
        _set_auth_cookies(response, result.refresh_token)
    return payload


@router.get("/me")
async def me(ctx: AuthContext = Depends(require(allow_password_change_pending=True))):
    return {"user": {
        "id": ctx.user["id"], "email": ctx.user["email"], "name": ctx.user["name"], "roles": ctx.user["roles"],
        "must_change_password": ctx.must_change_password, "claims": ctx.claims, "session_id": ctx.session_id,
    }}


@router.post("/logout")
async def logout(response: Response, ctx: AuthContext = Depends(require(allow_password_change_pending=True))):
    await sessions.revoke_session(ctx.session_id, "logout")
    _clear_auth_cookies(response)
    await write_audit(**ctx.actor(), action="AUTH_LOGOUT", entity_type="USER_SESSION",
                      entity_id=ctx.session_id, status="SUCCESS")
    return {"success": True}


@router.post("/change-password")
async def change_password(request: Request, ctx: AuthContext = Depends(require(allow_password_change_pending=True))):
    body = await request.json()
    current = body.get("current_password") if isinstance(body, dict) else None
    new = body.get("new_password") if isinstance(body, dict) else None
    if not isinstance(current, str) or not isinstance(new, str):
        raise ApiError(400, "Solicitud inválida")

    user = await users.find_by_id(ctx.user["id"])
    ok, _ = verify_password(current, (user or {}).get("password_hash"))
    if not ok:
        await write_audit(**ctx.actor(), action="AUTH_PASSWORD_CHANGED", entity_type="USER",
                          entity_id=ctx.user["id"], status="DENIED", error_message="Contraseña actual incorrecta")
        raise ApiError(400, "La contraseña actual no es correcta")
    if err := password_policy_error(new):
        raise ApiError(400, err)
    if current == new:
        raise ApiError(400, "La nueva contraseña debe ser distinta de la actual")

    await users.set_password_plain(ctx.user["id"], new, must_change=False)
    await write_audit(**ctx.actor(), action="AUTH_PASSWORD_CHANGED", entity_type="USER",
                      entity_id=ctx.user["id"], status="SUCCESS")
    return {"success": True, "message": "Contraseña actualizada."}
