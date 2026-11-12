from fastapi import APIRouter, Depends, Query

from app.api.deps import AuthContext, require
from app.core.audit import verify_chain, write_audit
from app.core.db import get_db
from app.services.audit_labels import human_readable

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/logs")
async def logs(limit: int = Query(500, ge=1, le=2000), ctx: AuthContext = Depends(require("audit:read"))):
    items = [
        {**doc, "human_readable": human_readable(doc.get("action", ""))}
        async for doc in get_db()["audit_logs"].find({}, projection={"_id": 0}).sort("seq_num", -1).limit(limit)
    ]
    # La integridad se comprueba en /audit/verify; aquí no se afirma nada.
    return {"total": len(items), "integrity_status": "NOT_VERIFIED", "logs": items}


@router.get("/verify")
async def verify(ctx: AuthContext = Depends(require("audit:read"))):
    result = await verify_chain()
    await write_audit(**ctx.actor(), action="AUDIT_INTEGRITY_VERIFIED", entity_type="AUDIT", entity_id="audit_logs",
                      status="SUCCESS" if result["ok"] else "FAILURE",
                      details={"verified_count": result["verified_count"], "first_broken_seq": result["first_broken_seq"],
                               "reason": result.get("reason")})
    return {"integrity_status": "VERIFIED" if result["ok"] else "BROKEN", **result}
