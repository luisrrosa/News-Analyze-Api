"""Esquemas de los cuerpos de las peticiones, para el contrato OpenAPI.

Los endpoints validan el cuerpo ellos mismos (mensajes en español y reglas de negocio); estos modelos documentan
qué campos aceptan, con ejemplos, para que la documentación y los escáneres de seguridad (OWASP ZAP) envíen
peticiones realistas. Se publican con `openapi_extra=body(Modelo)` en cada ruta.
"""
from typing import Any, Literal

from pydantic import BaseModel, Field


def _inline(node, defs: dict):
    """Sustituye las referencias internas ($defs) por su definición: el esquema queda autocontenido."""
    if isinstance(node, dict):
        ref = node.get("$ref", "")
        if ref.startswith("#/$defs/"):
            return _inline(defs[ref.split("/")[-1]], defs)
        return {k: _inline(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def body(model: type[BaseModel], content_type: str = "application/json") -> dict:
    schema = model.model_json_schema()
    schema = _inline(schema, schema.get("$defs", {}))
    return {"requestBody": {"required": True, "content": {content_type: {"schema": schema}}}}


def multipart_file(field: str = "file", description: str = "Archivo PDF") -> dict:
    return {"requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
        "type": "object", "required": [field],
        "properties": {field: {"type": "string", "format": "binary", "description": description}}}}}}}


class LoginIn(BaseModel):
    email: str = Field(examples=["editor@institucion.gob.do"], max_length=254)
    password: str = Field(examples=["Clave-Segura-2026"], max_length=256)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(examples=["Clave-Temporal-1"])
    new_password: str = Field(examples=["Nueva-Clave-Segura-2026"], description="Mínimo 12 caracteres, mayúsculas, minúsculas y dígitos")


class UserCreateIn(BaseModel):
    name: str = Field(examples=["María Pérez"], max_length=120)
    email: str = Field(examples=["maria.perez@institucion.gob.do"])
    roles: list[Literal["admin", "editor", "consulta", "auditor"]] = Field(examples=[["editor"]])
    password: str = Field(examples=["Clave-Inicial-2026"], description="Clave temporal; se exige cambiarla al entrar")


class UserUpdateIn(BaseModel):
    name: str | None = Field(None, examples=["María Pérez Gómez"])
    roles: list[Literal["admin", "editor", "consulta", "auditor"]] | None = Field(None, examples=[["consulta"]])
    status: Literal["active", "inactive"] | None = Field(None, examples=["inactive"])


class RoleClaimsIn(BaseModel):
    role: Literal["admin", "editor", "consulta", "auditor"] = Field(examples=["consulta"])
    claims: list[str] = Field(examples=[["dashboard:read", "news:read", "documents:read", "catalogs:read"]])


class MediaIn(BaseModel):
    name: str = Field(examples=["Diario Libre"], max_length=120)
    aliases: list[str] = Field(default_factory=list, examples=[["diario libre", "dl"]])
    domain: str | None = Field(None, examples=["diariolibre.com"])
    status: Literal["active", "inactive"] = "active"


class OfficeholderIn(BaseModel):
    position: str = Field(examples=["Ministro de Hacienda"])
    person_name: str = Field(examples=["Juan Pérez"])
    institution: str | None = Field(None, examples=["Ministerio de Hacienda y Economía"])
    state_power: Literal["executive", "legislative", "judicial", "other", "none"] = "executive"
    term_start: str = Field(examples=["2024-08-16"], description="AAAA-MM-DD")
    term_end: str | None = Field(None, examples=["2028-08-16"])
    source: str | None = Field(None, examples=["Gaceta Oficial"])
    verified_at: str | None = Field(None, examples=["2026-11-01"])


class ConfigIn(BaseModel):
    action: Literal["update", "test_ftp"] = Field(examples=["update"])
    section: Literal["smtp", "ftp"] | None = Field(None, examples=["smtp"])
    data: dict[str, Any] | None = Field(None, examples=[{"host": "smtp.institucion.gob.do", "port": 587, "enabled": False}])


class SamplingIn(BaseModel):
    sampling_rate: float = Field(ge=0, le=1, examples=[0.05], description="Proporción de noticias que pasan a muestreo")


class ProposeIn(BaseModel):
    changes: dict[str, Any] = Field(examples=[{"title": "TITULAR CORREGIDO", "source_destaque": "Alto"}])
    justification: str = Field(min_length=10, examples=["El titular extraído estaba cortado en la página 3."])


class JustificationIn(BaseModel):
    justification: str = Field("", examples=["La extracción es correcta según el PDF."])


class NoteIn(BaseModel):
    note: str = Field("", examples=["Revisado contra el original."])


class DraftIn(BaseModel):
    notes: str = Field("", examples=["Ajuste de sinónimos de seguridad ciudadana"])


class NotesIn(BaseModel):
    notes: str = Field(examples=["Motivo de los cambios del borrador"])


class TopicIn(BaseModel):
    name: str = Field(examples=["Seguridad Ciudadana y Reforma Policial"], max_length=120)
    synonyms: list[str] = Field(default_factory=list, examples=[["delincuencia", "policía nacional"]])
    active: bool = True
    description: str = ""
    scope: str = ""
    inclusion_criteria: str = ""
    exclusion_criteria: str = ""


class TriageFactor(BaseModel):
    code: str = Field(examples=["impacto_poblacion"])
    name: str = Field(examples=["Impacto en la población"])
    weight: int = Field(examples=[25])
    source_dimensions: list[str] = Field(default_factory=list)


class TriageLevel(BaseModel):
    code: str = Field(examples=["critico"])
    name: str = Field(examples=["Crítico"])
    min_score: int = Field(examples=[85])


class TriageIn(BaseModel):
    factors: list[TriageFactor]
    levels: list[TriageLevel]
    min_applicable_factors: int = Field(4, examples=[4])
    review_confidence_below: float = Field(0.6, examples=[0.6])
    exceptional_criteria: list[str] = Field(default_factory=list)


class ApproveRulesIn(BaseModel):
    updated_at: str | None = Field(None, examples=["2026-11-13T10:00:00.000Z"], description="Versión del borrador que se revisó")
