from datetime import date

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.api.dates import day_range
from app.api.deps import ApiError, AuthContext, require
from app.core.audit import write_audit
from app.services.report_pdf import build_presidential_report
from app.services.stats import summary_dates

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/presidential")
async def presidential_report(date_from: str | None = None, date_to: str | None = None,
                              ctx: AuthContext = Depends(require("reports:export"))):
    """PDF formal del período (por fecha del resumen). Sin fechas: último día con datos."""
    d1, d2 = day_range(date_from, date_to)
    if not d1 or not d2:
        rows = await summary_dates(limit=1)
        if not rows:
            raise ApiError(404, "No hay resúmenes procesados para generar el reporte", "NO_DATA")
        d1 = d1 or date.fromisoformat(rows[0]["date"])
        d2 = d2 or d1
    pdf = await build_presidential_report(d1.isoformat(), d2.isoformat(), ctx.user["name"] or ctx.user["email"])
    await write_audit(**ctx.actor(), action="REPORT_EXPORTED", entity_type="REPORT", entity_id="presidential",
                      status="SUCCESS", details={"date_from": d1.isoformat(), "date_to": d2.isoformat(),
                                                 "format": "pdf", "bytes": len(pdf)})
    name = f"Reporte_Presidencial_{d1.isoformat()}" + (f"_a_{d2.isoformat()}" if d2 != d1 else "") + ".pdf"
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
