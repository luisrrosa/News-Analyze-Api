"""Catálogos de edición directa (con auditoría antes/después): medios y alias; cargos y funcionarios."""
import re
import uuid
from datetime import date

from pymongo.errors import DuplicateKeyError

from app.core.db import get_db, iso_now

# Medios del corpus de referencia y sus alias observados (antes fijos en el extractor).
SEED_MEDIA = [
    {"name": "Diario Libre", "aliases": ["diario libre"]},
    {"name": "Listín Diario", "aliases": ["listín diario", "listin diario"]},
    {"name": "Hoy", "aliases": ["hoy"]},
    {"name": "El Día", "aliases": ["el día", "el día (diario)", "el dia", "el dia (diario)"]},
    {"name": "El Caribe", "aliases": ["el caribe"]},
    {"name": "El Nuevo Diario", "aliases": ["el nuevo diario"]},
]
STATE_POWERS = ["executive", "legislative", "judicial", "public_ministry", "constitutional_organs", "none"]


class CatalogError(Exception):
    pass


def _clean(v: object, maxlen: int = 200) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:maxlen]


async def ensure_seed_media() -> None:
    col = get_db()["media_sources"]
    if await col.count_documents({}, limit=1):
        return
    now = iso_now()
    for m in SEED_MEDIA:
        try:
            await col.insert_one({"id": f"med_{uuid.uuid4()}", "name": m["name"], "aliases": m["aliases"],
                                  "domain": None, "status": "active", "created_at": now, "updated_at": now})
        except DuplicateKeyError:
            pass


async def media_alias_map() -> dict[str, str]:
    """alias normalizado → nombre canónico (medios activos). Lo usa el extractor."""
    out: dict[str, str] = {}
    async for m in get_db()["media_sources"].find({"status": "active"}):
        out[m["name"].lower()] = m["name"]
        for a in m.get("aliases", []):
            out[a.lower()] = m["name"]
    return out


async def list_media() -> list[dict]:
    return [m async for m in get_db()["media_sources"].find({}, projection={"_id": 0}).sort("name", 1)]


def _media_payload(data: dict) -> dict:
    name = _clean(data.get("name"), 120)
    if not name:
        raise CatalogError("El medio requiere un nombre")
    aliases = data.get("aliases") or []
    if not isinstance(aliases, list):
        raise CatalogError("Los alias deben ser una lista")
    domain = _clean(data.get("domain"), 120).lower() or None
    if domain and not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain):
        raise CatalogError("Dominio inválido (ej. diariolibre.com)")
    status = data.get("status", "active")
    if status not in ("active", "inactive"):
        raise CatalogError("Estado inválido")
    return {"name": name, "aliases": sorted({_clean(a, 120).lower() for a in aliases if _clean(a, 120)}),
            "domain": domain, "status": status}


async def _check_alias_conflicts(payload: dict, exclude_id: str | None) -> None:
    keys = {payload["name"].lower(), *payload["aliases"]}
    async for m in get_db()["media_sources"].find({"id": {"$ne": exclude_id}} if exclude_id else {}):
        taken = {m["name"].lower(), *m.get("aliases", [])} & keys
        if taken:
            raise CatalogError(f"«{', '.join(sorted(taken))}» ya pertenece a {m['name']}")


async def save_media(media_id: str | None, data: dict) -> tuple[dict | None, dict]:
    payload = _media_payload(data)
    await _check_alias_conflicts(payload, media_id)
    col = get_db()["media_sources"]
    now = iso_now()
    if media_id:
        before = await col.find_one({"id": media_id}, projection={"_id": 0})
        if not before:
            raise CatalogError("Medio no encontrado")
        await col.update_one({"id": media_id}, {"$set": {**payload, "updated_at": now}})
        return before, await col.find_one({"id": media_id}, projection={"_id": 0})
    doc = {"id": f"med_{uuid.uuid4()}", **payload, "created_at": now, "updated_at": now}
    try:
        await col.insert_one(dict(doc))
    except DuplicateKeyError:
        raise CatalogError("Ya existe un medio con ese nombre")
    return None, doc


async def list_officeholders() -> list[dict]:
    return [o async for o in get_db()["officeholders"].find({}, projection={"_id": 0})
            .sort([("position", 1), ("term_start", -1)])]


def _parse_day(v: object, field: str, required: bool) -> str | None:
    s = _clean(v, 10)
    if not s:
        if required:
            raise CatalogError(f"{field} es obligatoria")
        return None
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise CatalogError(f"{field} inválida (AAAA-MM-DD)")


async def save_officeholder(oid: str | None, data: dict, user_id: str) -> tuple[dict | None, dict]:
    position, person = _clean(data.get("position"), 160), _clean(data.get("person_name"), 160)
    if not position or not person:
        raise CatalogError("Cargo y persona son obligatorios")
    start = _parse_day(data.get("term_start"), "Fecha de inicio", True)
    end = _parse_day(data.get("term_end"), "Fecha de fin", False)
    if end and end < start:
        raise CatalogError("La fecha de fin es anterior a la de inicio")
    power = data.get("state_power") or "none"
    if power not in STATE_POWERS:
        raise CatalogError("Poder del Estado inválido")
    payload = {"position": position, "person_name": person, "institution": _clean(data.get("institution"), 160) or None,
               "state_power": power, "term_start": start, "term_end": end,
               "source": _clean(data.get("source"), 300) or None,
               "verified_at": _parse_day(data.get("verified_at"), "Fecha de verificación", False)}
    col = get_db()["officeholders"]
    # Un cargo no puede tener dos titulares con períodos solapados.
    async for o in col.find({"position": {"$regex": f"^{re.escape(position)}$", "$options": "i"}, "id": {"$ne": oid}}):
        o_end = o.get("term_end") or "9999-12-31"
        if start <= o_end and (end or "9999-12-31") >= o["term_start"]:
            raise CatalogError(f"El período se solapa con {o['person_name']} ({o['term_start']} – {o.get('term_end') or 'vigente'})")
    now = iso_now()
    if oid:
        before = await col.find_one({"id": oid}, projection={"_id": 0})
        if not before:
            raise CatalogError("Registro no encontrado")
        await col.update_one({"id": oid}, {"$set": {**payload, "updated_by": user_id, "updated_at": now}})
        return before, await col.find_one({"id": oid}, projection={"_id": 0})
    doc = {"id": f"off_{uuid.uuid4()}", **payload, "created_by": user_id, "created_at": now,
           "updated_by": user_id, "updated_at": now}
    await col.insert_one(dict(doc))
    return None, doc


async def holder_on(position: str, on_day: str) -> dict | None:
    """Titular de un cargo en una fecha (para resolver «el presidente» por fecha de publicación, Fase 3)."""
    q = {"position": {"$regex": f"^{re.escape(position)}$", "$options": "i"}, "term_start": {"$lte": on_day},
         "$or": [{"term_end": None}, {"term_end": {"$gte": on_day}}]}
    return await get_db()["officeholders"].find_one(q, projection={"_id": 0})
