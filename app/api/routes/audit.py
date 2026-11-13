import re

from fastapi import APIRouter, Depends, Query

from app.api.dates import timestamp_filter
from app.api.deps import AuthContext, require
from app.core.audit import verify_chain, write_audit
from app.core.db import get_db
from app.services.audit_labels import human_readable

router = APIRouter(prefix="/audit", tags=["audit"])

# Lo que se ve sin audit:read_detail: el detalle técnico (details, request_id, hashes completos) queda fuera.
_SUMMARY_FIELDS = {"_id": 0, "details": 0}


@router.get("/logs")
async def logs(page: int = Query(1, ge=1, le=100000), limit: int = Query(25, ge=1, le=200),
               date_from: str | None = None, date_to: str | None = None,
               user_email: str | None = Query(None, max_length=254), status: str | None = Query(None, pattern="^(SUCCESS|DENIED|FAILURE)$"),
               action: str | None = Query(None, max_length=80), q: str | None = Query(None, max_length=120),
               ctx: AuthContext = Depends(require("audit:read"))):
    """Paginado y filtrado en el servidor (antes se traían 2000 registros y se filtraba en el navegador)."""
    query: dict = timestamp_filter(date_from, date_to)
    if user_email:
        query["user_email"] = user_email
    if status:
        query["status"] = status
    if action:
        query["action"] = action
    if q and q.strip():
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        query["$or"] = [{"action": rx}, {"user_email": rx}, {"ip_address": rx}, {"entity_id": rx}, {"record_hash": rx}]

    detail = "audit:read_detail" in ctx.claims
    col = get_db()["audit_logs"]
    total = await col.count_documents(query)
    items = [
        {**doc, "human_readable": human_readable(doc.get("action", ""))}
        async for doc in col.find(query, projection={"_id": 0} if detail else _SUMMARY_FIELDS)
        .sort("seq_num", -1).skip((page - 1) * limit).limit(limit)
    ]
    # Usuarios del período para el filtro (lista corta, sin traer los registros).
    users = sorted(u for u in await col.distinct("user_email", timestamp_filter(date_from, date_to)) if u)[:500]
    return {"total": total, "page": page, "limit": limit, "logs": items, "users": users,
            "detail_included": detail, "integrity_status": "NOT_VERIFIED"}


@router.get("/verify")
async def verify(full: bool = False, ctx: AuthContext = Depends(require("audit:verify"))):
    result = await verify_chain(full=full)
    await write_audit(**ctx.actor(), action="AUDIT_INTEGRITY_VERIFIED", entity_type="AUDIT", entity_id="audit_logs",
                      status="SUCCESS" if result["ok"] else "FAILURE",
                      details={"verified_count": result["verified_count"], "first_broken_seq": result["first_broken_seq"],
                               "reason": result.get("reason"), "full": full})
    return {"integrity_status": "VERIFIED" if result["ok"] else "BROKEN", **result}
