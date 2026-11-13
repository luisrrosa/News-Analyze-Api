from fastapi import APIRouter, Depends, Request

from app.api.schemas import RoleClaimsIn, body
from app.api.deps import ApiError, AuthContext, require, require_any
from app.core.audit import write_audit
from app.core.rbac import ADMIN_LOCKED_CLAIMS, ALL_CLAIMS, PRIVILEGED_CLAIMS, SYSTEM_ROLES
from app.services import users

router = APIRouter(prefix="/roles", tags=["roles"])


@router.get("")
async def get_roles(ctx: AuthContext = Depends(require_any("roles:manage", "users:read"))):
    # La interfaz dibuja la matriz con este catálogo (no tiene copia propia).
    return {"role_claims": await users.role_claims_map(), "all_claims": ALL_CLAIMS,
            "admin_locked_claims": ADMIN_LOCKED_CLAIMS, "privileged_claims": sorted(PRIVILEGED_CLAIMS),
            "can_grant_privileged": "admin" in (ctx.user.get("roles") or [])}


@router.post("", openapi_extra=body(RoleClaimsIn))
async def set_role_claims(request: Request, ctx: AuthContext = Depends(require("roles:manage"))):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    role = body.get("role") if isinstance(body, dict) else None
    claims = body.get("claims") if isinstance(body, dict) else None
    if role not in SYSTEM_ROLES or not isinstance(claims, list) or not all(isinstance(c, str) for c in claims):
        raise ApiError(400, "Parámetros inválidos (role y claims[] requeridos)")
    current = (await users.role_claims_map()).get(role, [])
    changed_privileged = (set(claims) ^ set(current)) & PRIVILEGED_CLAIMS
    if (changed_privileged or role == "admin") and "admin" not in (ctx.user.get("roles") or []):
        raise ApiError(403, "Solo un administrador puede modificar el rol admin o los permisos de seguridad: "
                            + ", ".join(sorted(changed_privileged)), "FORBIDDEN")
    try:
        before, after = await users.set_role_claims(role, claims)
    except users.UserError as e:
        raise ApiError(400, str(e))
    await write_audit(**ctx.actor(), action="ROLE_CLAIMS_UPDATED", entity_type="ROLE", entity_id=role, status="SUCCESS",
                      details={"claims_added": [c for c in after if c not in before],
                               "claims_removed": [c for c in before if c not in after]})
    return {"success": True, "role": role, "role_claims": await users.role_claims_map(),
            "message": f"Matriz de claims actualizada para el rol '{role}'"}
