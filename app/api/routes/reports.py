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


async def _range_or_latest(date_from: str | None, date_to: str | None) -> tuple[date, date]:
    d1, d2 = day_range(date_from, date_to)
    if not d1 or not d2:
        rows = await summary_dates(limit=1)
        if not rows:
            raise ApiError(404, "No hay resúmenes procesados para generar el reporte", "NO_DATA")
        d1 = d1 or date.fromisoformat(rows[0]["date"])
        d2 = d2 or d1
    return d1, d2


def _pdf_response(pdf: bytes, prefix: str, d1: date, d2: date) -> Response:
    safe = "".join(c if c.isalnum() else "_" for c in prefix)[:60]
    name = f"{safe}_{d1.isoformat()}" + (f"_a_{d2.isoformat()}" if d2 != d1 else "") + ".pdf"
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.get("/media")
async def media_report(media: str, date_from: str | None = None, date_to: str | None = None,
                       ctx: AuthContext = Depends(require("reports:export"))):
    """Reporte del período limitado a un medio."""
    d1, d2 = await _range_or_latest(date_from, date_to)
    pdf = await build_presidential_report(d1.isoformat(), d2.isoformat(), ctx.user["name"] or ctx.user["email"],
                                          extra_match={"media_name": media}, title="Reporte de Prensa por Medio",
                                          scope=f"Medio: {media}")
    await write_audit(**ctx.actor(), action="REPORT_EXPORTED", entity_type="REPORT", entity_id="media",
                      status="SUCCESS", details={"media": media, "date_from": d1.isoformat(), "date_to": d2.isoformat(),
                                                 "format": "pdf"})
    return _pdf_response(pdf, f"Reporte_{media}", d1, d2)


@router.get("/topic")
async def topic_report(topic: str, date_from: str | None = None, date_to: str | None = None,
                       ctx: AuthContext = Depends(require("reports:export"))):
    """Reporte del período limitado a un tema del catálogo (clasificación provisional)."""
    from app.services.rules import get_effective
    rule_set, _ = await get_effective()
    name = next((t["name"] for t in (rule_set or {}).get("topics", []) if t["code"] == topic), None)
    if not name:
        raise ApiError(404, "Tema no encontrado en el catálogo vigente")
    d1, d2 = await _range_or_latest(date_from, date_to)
    pdf = await build_presidential_report(d1.isoformat(), d2.isoformat(), ctx.user["name"] or ctx.user["email"],
                                          extra_match={"provisional_topics": topic}, title="Reporte de Prensa por Tema",
                                          scope=f"Tema: {topic} {name} (clasificación provisional por sinónimos, "
                                                f"reglas {(rule_set or {}).get('version')})")
    await write_audit(**ctx.actor(), action="REPORT_EXPORTED", entity_type="REPORT", entity_id="topic",
                      status="SUCCESS", details={"topic": topic, "date_from": d1.isoformat(), "date_to": d2.isoformat(),
                                                 "format": "pdf"})
    return _pdf_response(pdf, f"Reporte_tema_{topic}", d1, d2)
