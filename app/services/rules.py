"""Reglas versionadas: catálogo temático y configuración de triage (Plan V3.0 §4.4, §11; docs 03 y 05).

- Una versión por documento en `rule_sets` (RS-AAAA.MM-Vx.y). Estados: draft → approved → retired.
- Solo existe un borrador editable a la vez; al aprobarlo pasa a vigente y el vigente anterior queda retirado.
- La versión inicial es un BORRADOR: los 32 temas son provisionales (D-01) y los pesos/umbrales del doc 05
  son una propuesta (D-04). El triage permanece en modo simulación aunque la versión se apruebe,
  salvo que se cambie explícitamente el modo en una versión aprobada.
"""
import re
import unicodedata
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError

from app.core.db import get_db, iso_now

PROVISIONAL_TOPICS = [
    "Seguridad Ciudadana y Reforma Policial", "Salud Pública y Salud Mental", "Economía, Inflación y Costo de Vida",
    "Transparencia, Contrataciones y Ética", "Educación Preuniversitaria y Superior", "Energía y Sector Eléctrico",
    "Agua, Recursos Hídricos y Medio Ambiente", "Transporte y Obras Públicas", "Relaciones Exteriores y Frontera",
    "Justicia y Ministerio Público", "Agricultura y Seguridad Alimentaria", "Turismo y Marca País",
    "Empleo y Seguridad Social", "Industria, Zonas Francas y Comercio", "Vivienda y Hábitat",
    "Cultura y Patrimonio Nacional", "Juventud y Deportes", "Defensa y Fuerzas Armadas",
    "Políticas Sociales y Supérate", "Comunicaciones y Tecnología", "Sector Financiero y Banca Central",
    "Régimen Electoral y Partidos", "Gobiernos Locales y Ayuntamientos", "Derechos Humanos e Inclusión",
    "Asuntos Legislativos y Reformas", "Tratados y Acuerdos Comerciales", "Prevención y Gestión de Riesgos",
    "Desarrollo Fronterizo", "Niñez, Adolescencia y Familia", "Cooperación Internacional",
    "Inversión Extranjera Directa", "Innovación y Gobierno Digital",
]

# Doc 05 (pesos y niveles propuestos) + Plan V3.0 §11.2 (correspondencia con dimensiones del doc 04).
DEFAULT_TRIAGE = {
    "mode": "simulation",
    "scale": "1-5",
    "min_applicable_factors": 4,
    "factors": [
        {"code": "impacto_poblacion", "name": "Impacto en la población", "weight": 25,
         "source_dimensions": [{"dimension": "social", "weight": 0.6}, {"dimension": "economica", "weight": 0.4}]},
        {"code": "urgencia_temporal", "name": "Urgencia temporal", "weight": 20, "source_dimensions": []},
        {"code": "riesgo_institucional", "name": "Riesgo institucional y gobernabilidad", "weight": 20,
         "source_dimensions": [{"dimension": "institucional", "weight": 0.6}, {"dimension": "politica", "weight": 0.4}]},
        {"code": "impacto_economico", "name": "Impacto económico", "weight": 10,
         "source_dimensions": [{"dimension": "economica", "weight": 1.0}]},
        {"code": "alcance_reputacional", "name": "Alcance reputacional", "weight": 10,
         "source_dimensions": [{"dimension": "reputacional", "weight": 1.0}]},
        {"code": "escalamiento_estrategico", "name": "Potencial de escalamiento estratégico", "weight": 15,
         "source_dimensions": [{"dimension": "estrategica", "weight": 1.0}]},
    ],
    "levels": [
        {"code": "critico", "name": "Crítico", "min_score": 85},
        {"code": "alto", "name": "Alto", "min_score": 70},
        {"code": "medio", "name": "Medio", "min_score": 50},
        {"code": "bajo", "name": "Bajo", "min_score": 30},
        {"code": "informativo", "name": "Informativo", "min_score": 0},
    ],
    "coherence_max_gap": 2,
    "review_confidence_below": 0.65,
    "exceptional_criteria": [
        "Amenaza directa a la vida", "Desastre o emergencia grave", "Seguridad nacional",
        "Interrupción de servicios esenciales", "Crisis constitucional", "Violencia grave",
        "Vulneración grave de derechos fundamentales",
    ],
}
DIMENSIONS = ["institucional", "politica", "social", "economica", "reputacional", "estrategica"]


class RuleError(Exception):
    pass


def _slug(text: str) -> str:
    t = unicodedata.normalize("NFD", text.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "_", t).strip("_")[:40]


def derive_synonyms(name: str) -> list[str]:
    """Sinónimos iniciales a partir del nombre del tema (transparente y editable)."""
    parts = re.split(r",|\s+y\s+|\s+e\s+", name)
    return [p.strip().lower() for p in parts if len(p.strip()) > 3]


def default_topics() -> list[dict]:
    return [{
        "code": f"T{i:02d}", "name": name, "description": "", "scope": "",
        "inclusion_criteria": "", "exclusion_criteria": "",
        "synonyms": derive_synonyms(name), "active": True,
    } for i, name in enumerate(PROVISIONAL_TOPICS, start=1)]


def _version_label(major: int, minor: int, when: datetime | None = None) -> str:
    w = when or datetime.now(timezone.utc)
    return f"RS-{w.year}.{w.month:02d}-V{major}.{minor}"


def _public(rs: dict | None) -> dict | None:
    if rs is None:
        return None
    return {k: v for k, v in rs.items() if k != "_id"}


async def ensure_initial_rule_set() -> None:
    col = get_db()["rule_sets"]
    if await col.count_documents({}, limit=1):
        return
    now = iso_now()
    try:
        await col.insert_one({
            "version": _version_label(0, 3), "major": 0, "minor": 3, "status": "draft",
            "based_on": None,
            "notes": "Versión inicial: catálogo provisional de 32 temas (no institucional, D-01) y triage V0.3 del "
                     "documento 05 en modo simulación (D-04). Pendiente de validación y aprobación del propietario.",
            "topics": default_topics(), "triage": DEFAULT_TRIAGE,
            "created_by": "bootstrap", "created_at": now, "updated_by": "bootstrap", "updated_at": now,
            "approved_by": None, "approved_at": None, "valid_from": None, "valid_to": None,
        })
    except DuplicateKeyError:
        pass


async def list_versions() -> list[dict]:
    proj = {"_id": 0, "version": 1, "status": 1, "notes": 1, "created_by": 1, "created_at": 1, "approved_by": 1,
            "approved_at": 1, "valid_from": 1, "valid_to": 1, "based_on": 1}
    return [d async for d in get_db()["rule_sets"].find({}, projection=proj).sort([("major", -1), ("minor", -1)])]


async def get_version(version: str) -> dict | None:
    return _public(await get_db()["rule_sets"].find_one({"version": version}))


async def get_active() -> dict | None:
    return _public(await get_db()["rule_sets"].find_one({"status": "approved"}, sort=[("approved_at", -1)]))


async def get_draft() -> dict | None:
    return _public(await get_db()["rule_sets"].find_one({"status": "draft"}))


async def get_effective() -> tuple[dict | None, bool]:
    """Versión que usan las vistas: la vigente o, si no hay ninguna aprobada, el borrador (marcado como tal)."""
    active = await get_active()
    if active:
        return active, True
    return await get_draft(), False


async def create_draft(user_id: str, notes: str) -> dict:
    col = get_db()["rule_sets"]
    if await col.find_one({"status": "draft"}):
        raise RuleError("Ya existe un borrador en edición")
    base = await get_active()
    if base is None:
        raise RuleError("No hay versión vigente de la que partir")
    last = await col.find_one({}, sort=[("major", -1), ("minor", -1)])
    major, minor = last["major"], last["minor"] + 1
    now = iso_now()
    doc = {
        "version": _version_label(major, minor), "major": major, "minor": minor, "status": "draft",
        "based_on": base["version"], "notes": notes.strip()[:2000],
        "topics": base["topics"], "triage": base["triage"],
        "created_by": user_id, "created_at": now, "updated_by": user_id, "updated_at": now,
        "approved_by": None, "approved_at": None, "valid_from": None, "valid_to": None,
    }
    await col.insert_one(dict(doc))
    return doc


async def discard_draft() -> str:
    draft = await get_draft()
    if not draft:
        raise RuleError("No hay borrador")
    if not await get_active():
        raise RuleError("El borrador inicial no se puede descartar mientras no exista una versión aprobada")
    await get_db()["rule_sets"].delete_one({"version": draft["version"], "status": "draft"})
    return draft["version"]


async def _update_draft(changes: dict, user_id: str) -> dict:
    draft = await get_draft()
    if not draft:
        raise RuleError("No hay borrador: cree una nueva versión para editar")
    await get_db()["rule_sets"].update_one(
        {"version": draft["version"], "status": "draft"},
        {"$set": {**changes, "updated_by": user_id, "updated_at": iso_now()}})
    return await get_draft()  # type: ignore[return-value]


def _clean_topic(t: dict) -> dict:
    name = str(t.get("name", "")).strip()
    if not name or len(name) > 120:
        raise RuleError("Cada tema requiere un nombre (máx. 120 caracteres)")
    synonyms = t.get("synonyms") or []
    if not isinstance(synonyms, list) or not all(isinstance(x, str) for x in synonyms):
        raise RuleError(f"Sinónimos inválidos en «{name}»")
    syn = sorted({x.strip().lower() for x in synonyms if 2 < len(x.strip()) <= 80})
    out = {"name": name, "active": bool(t.get("active", True)), "synonyms": syn}
    for f in ("description", "scope", "inclusion_criteria", "exclusion_criteria"):
        out[f] = str(t.get(f, "") or "").strip()[:2000]
    return out


async def save_topic(code: str | None, data: dict, user_id: str) -> dict:
    draft = await get_draft()
    if not draft:
        raise RuleError("No hay borrador: cree una nueva versión para editar")
    topics = list(draft["topics"])
    clean = _clean_topic(data)
    if code:
        idx = next((i for i, t in enumerate(topics) if t["code"] == code), None)
        if idx is None:
            raise RuleError("Tema no encontrado")
        topics[idx] = {**topics[idx], **clean, "code": code}  # el código es estable: no se cambia
    else:
        used = {t["code"] for t in topics}
        n = len(topics) + 1
        while f"T{n:02d}" in used:
            n += 1
        topics.append({**clean, "code": f"T{n:02d}"})
    if len({t["name"].lower() for t in topics}) != len(topics):
        raise RuleError("Ya existe un tema con ese nombre")
    return await _update_draft({"topics": topics}, user_id)


def validate_triage(cfg: dict) -> dict:
    factors = cfg.get("factors")
    levels = cfg.get("levels")
    if not isinstance(factors, list) or not factors:
        raise RuleError("Debe haber al menos un factor")
    codes, total = set(), 0.0
    clean_f = []
    for f in factors:
        code = _slug(str(f.get("code") or f.get("name") or ""))
        name = str(f.get("name", "")).strip()
        try:
            weight = float(f.get("weight"))
        except (TypeError, ValueError):
            raise RuleError(f"Peso inválido en «{name}»")
        if not code or not name or code in codes:
            raise RuleError("Cada factor necesita código y nombre únicos")
        if not 0 <= weight <= 100:
            raise RuleError(f"El peso de «{name}» debe estar entre 0 y 100")
        dims = []
        for d in f.get("source_dimensions") or []:
            if d.get("dimension") not in DIMENSIONS:
                raise RuleError(f"Dimensión desconocida en «{name}»")
            dims.append({"dimension": d["dimension"], "weight": round(float(d.get("weight", 0)), 4)})
        if dims and abs(sum(d["weight"] for d in dims) - 1) > 0.001:
            raise RuleError(f"Las dimensiones de origen de «{name}» deben sumar 1")
        codes.add(code)
        total += weight
        clean_f.append({"code": code, "name": name, "weight": weight, "source_dimensions": dims})
    if abs(total - 100) > 0.001:
        raise RuleError(f"Los pesos deben sumar 100 % (suman {total:g} %)")
    if not isinstance(levels, list) or not levels:
        raise RuleError("Debe haber niveles de triage")
    clean_l = sorted(({"code": _slug(str(l.get("code") or l.get("name"))), "name": str(l.get("name", "")).strip(),
                       "min_score": float(l.get("min_score", -1))} for l in levels), key=lambda l: -l["min_score"])
    scores = [l["min_score"] for l in clean_l]
    if len(set(scores)) != len(scores) or any(not 0 <= s <= 100 for s in scores) or scores[-1] != 0:
        raise RuleError("Los umbrales deben ser distintos, entre 0 y 100, y el último nivel debe empezar en 0")
    mode = cfg.get("mode", "simulation")
    if mode not in ("simulation", "active"):
        raise RuleError("Modo de triage inválido")
    criteria = [str(c).strip()[:200] for c in (cfg.get("exceptional_criteria") or []) if str(c).strip()]
    return {
        "mode": mode, "scale": "1-5",
        "min_applicable_factors": max(1, min(len(clean_f), int(cfg.get("min_applicable_factors", 4)))),
        "factors": clean_f, "levels": clean_l,
        "coherence_max_gap": float(cfg.get("coherence_max_gap", 2)),
        "review_confidence_below": max(0.0, min(1.0, float(cfg.get("review_confidence_below", 0.65)))),
        "exceptional_criteria": criteria,
    }


async def save_triage(cfg: dict, user_id: str) -> dict:
    return await _update_draft({"triage": validate_triage(cfg)}, user_id)


async def update_notes(notes: str, user_id: str) -> dict:
    return await _update_draft({"notes": notes.strip()[:2000]}, user_id)


async def approve_draft(user_id: str) -> tuple[dict, str | None]:
    draft = await get_draft()
    if not draft:
        raise RuleError("No hay borrador para aprobar")
    validate_triage(draft["triage"])
    if not any(t["active"] for t in draft["topics"]):
        raise RuleError("El catálogo debe tener al menos un tema activo")
    col = get_db()["rule_sets"]
    now = iso_now()
    previous = await get_active()
    if previous:
        await col.update_one({"version": previous["version"]}, {"$set": {"status": "retired", "valid_to": now}})
    await col.update_one({"version": draft["version"], "status": "draft"},
                         {"$set": {"status": "approved", "approved_by": user_id, "approved_at": now, "valid_from": now}})
    return await get_version(draft["version"]), previous["version"] if previous else None  # type: ignore[return-value]


def diff_versions(a: dict, b: dict) -> dict:
    ta = {t["code"]: t for t in a.get("topics", [])}
    tb = {t["code"]: t for t in b.get("topics", [])}
    return {
        "topics_added": [tb[c]["name"] for c in tb if c not in ta],
        "topics_changed": [tb[c]["name"] for c in tb if c in ta and tb[c] != ta[c]],
        "topics_removed": [ta[c]["name"] for c in ta if c not in tb],
        "triage_changed": a.get("triage") != b.get("triage"),
    }
