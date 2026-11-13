import re
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.schemas import UserCreateIn, UserUpdateIn, body
from app.api.deps import ApiError, AuthContext, check_claim, require
from app.core.audit import write_audit
from app.core.passwords import password_policy_error
from app.core.rbac import SYSTEM_ROLES
from app.services import users

router = APIRouter(prefix="/users", tags=["users"])

STATUSES = {"active", "inactive", "locked", "pending"}
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _valid_roles(roles: object) -> bool:
    return isinstance(roles, list) and len(roles) > 0 and all(r in SYSTEM_ROLES for r in roles)


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


@router.get("")
async def list_users(ctx: AuthContext = Depends(require("users:read"))):
    return {"users": await users.list_users()}


@router.post("", status_code=201, openapi_extra=body(UserCreateIn))
async def create_user(request: Request, ctx: AuthContext = Depends(require("users:create"))):
    body = await _json(request)
    name, email, roles, password = body.get("name"), body.get("email"), body.get("roles"), body.get("password")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ApiError(400, "Nombre requerido (máx. 120 caracteres)")
    if not isinstance(email, str) or len(email) > 254 or not EMAIL_RE.match(email):
        raise ApiError(400, "Email inválido")
    if not _valid_roles(roles):
        raise ApiError(400, "Debe indicar al menos un rol válido (admin, editor, consulta, auditor)")
    if err := password_policy_error(password):
        raise ApiError(400, f"Contraseña inicial: {err}")
    try:
        user = await users.create_user(name=name, email=email, roles=roles, password=password, created_by=ctx.user["id"])
    except users.UserError as e:
        raise ApiError(400, str(e))
    await write_audit(**ctx.actor(), action="USER_CREATED", entity_type="USER", entity_id=user["id"], status="SUCCESS",
                      details={"target_email": user["email"], "target_name": user["name"], "roles": user["roles"]})
    return {"user": users.public_user(user),
            "message": "Usuario creado. Deberá cambiar la contraseña en su primer inicio de sesión."}


@router.get("/{user_id}")
async def get_user(user_id: str, ctx: AuthContext = Depends(require("users:read"))):
    user = await users.find_by_id(user_id)
    if not user:
        raise ApiError(404, "Usuario no encontrado")
    return {"user": users.public_user(user)}


@router.put("/{user_id}", openapi_extra=body(UserUpdateIn))
async def update_user(user_id: str, request: Request, ctx: AuthContext = Depends(require("users:read"))):
    body = await _json(request)
    name, roles, status = body.get("name"), body.get("roles"), body.get("status")
    if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 120):
        raise ApiError(400, "Nombre inválido")
    if roles is not None and not _valid_roles(roles):
        raise ApiError(400, "Roles inválidos")
    if status is not None and status not in STATUSES:
        raise ApiError(400, "Estado inválido")
    if name is not None:
        check_claim(ctx, "users:update")
    if roles is not None:
        check_claim(ctx, "users:assign_roles")
    if status is not None:
        check_claim(ctx, "users:deactivate")
    if user_id == ctx.user["id"] and ((roles is not None and "admin" not in roles) or (status is not None and status != "active")):
        raise ApiError(400, "No puede quitarse el rol admin ni desactivar su propia cuenta")
    target = await users.find_by_id(user_id)
    if not target:
        raise ApiError(404, "Usuario no encontrado")
    caller_is_admin = "admin" in (ctx.user.get("roles") or [])
    # Sin escalada: solo quien tiene el rol admin puede dar o quitar el rol admin, o modificar a un administrador.
    if not caller_is_admin and ("admin" in (target.get("roles") or []) or (roles is not None and "admin" in roles)):
        raise ApiError(403, "Solo un administrador puede asignar el rol admin o modificar a otro administrador", "FORBIDDEN")
    try:
        before, after = await users.update_user(user_id, name=name, roles=roles, status=status)
    except users.UserError as e:
        raise ApiError(400, str(e))
    await write_audit(**ctx.actor(), action="USER_ROLE_CLAIMS_MODIFIED", entity_type="USER", entity_id=user_id,
                      status="SUCCESS", details={
                          "target_email": after["email"],
                          "previous": {"name": before["name"], "roles": before["roles"], "status": before["status"]},
                          "new": {"name": after["name"], "roles": after["roles"], "status": after["status"]},
                      })
    return {"user": users.public_user(after), "message": f"Usuario {after['name']} actualizado."}


def _temp_password() -> str:
    upper, lower, digits = "ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnpqrstuvwxyz", "23456789"
    chars = [secrets.choice(upper), secrets.choice(lower), secrets.choice(digits)]
    chars += [secrets.choice(upper + lower + digits) for _ in range(13)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


@router.post("/{user_id}/reset-password")
async def reset_password(user_id: str, ctx: AuthContext = Depends(require("users:reset_password"))):
    """Sin servicio de correo aprobado (D-09): el admin recibe la clave temporal una sola vez (Plan V3.0 §7.3)."""
    user = await users.find_by_id(user_id)
    if not user:
        raise ApiError(404, "Usuario no encontrado")
    temp = _temp_password()
    await users.set_password_plain(user_id, temp, must_change=True)
    await users.revoke_all_sessions(user_id, "password_reset")
    await write_audit(**ctx.actor(), action="PASSWORD_RESET_REQUESTED", entity_type="USER", entity_id=user_id,
                      status="SUCCESS", details={"target_email": user["email"], "delivery": "mostrada_al_administrador",
                                                 "must_change_password": True})
    return JSONResponse(
        {"success": True, "temp_password": temp, "user_id": user_id, "user_email": user["email"],
         "message": "Clave temporal generada. Entréguela al usuario por un canal seguro: no se envió ningún correo. "
                    "Deberá cambiarla al entrar."},
        headers={"Cache-Control": "no-store"},
    )
