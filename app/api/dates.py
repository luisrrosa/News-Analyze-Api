"""Rango de fechas común a todas las vistas.

Noticias y documentos se filtran por summary_date (fecha del resumen, AAAA-MM-DD, siempre presente);
la auditoría, por la fecha del evento (timestamp_utc).
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.api.deps import ApiError


def parse_day(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ApiError(400, f"Fecha inválida en {name} (formato AAAA-MM-DD)", "INVALID_DATE")


def day_range(date_from: str | None, date_to: str | None) -> tuple[date | None, date | None]:
    d1, d2 = parse_day(date_from, "date_from"), parse_day(date_to, "date_to")
    if d1 and d2 and d1 > d2:
        raise ApiError(400, "La fecha inicial es posterior a la final", "INVALID_DATE_RANGE")
    if d1 and d2 and (d2 - d1).days > 366:
        raise ApiError(400, "El rango máximo es de un año", "DATE_RANGE_TOO_LONG")
    return d1, d2


def summary_date_filter(date_from: str | None, date_to: str | None) -> dict:
    d1, d2 = day_range(date_from, date_to)
    cond: dict = {}
    if d1:
        cond["$gte"] = d1.isoformat()
    if d2:
        cond["$lte"] = d2.isoformat()
    return {"summary_date": cond} if cond else {}


# Los días se interpretan en la hora local institucional (Plan V3.0 §12.4: almacenamiento UTC, presentación local).
LOCAL_TZ = ZoneInfo("America/Santo_Domingo")


def _utc_iso(d: date) -> str:
    """Inicio del día local d, expresado como cadena ISO UTC comparable con timestamp_utc."""
    return datetime.combine(d, time.min, LOCAL_TZ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def timestamp_filter(date_from: str | None, date_to: str | None) -> dict:
    """Auditoría: [00:00 del día from, 00:00 del día siguiente a to) en hora de Santo Domingo."""
    d1, d2 = day_range(date_from, date_to)
    cond: dict = {}
    if d1:
        cond["$gte"] = _utc_iso(d1)
    if d2:
        cond["$lt"] = _utc_iso(d2 + timedelta(days=1))
    return {"timestamp_utc": cond} if cond else {}
