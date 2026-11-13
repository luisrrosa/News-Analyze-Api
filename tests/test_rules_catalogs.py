"""Fase 2.5: reglas versionadas (temas y triage), medios, cargos y radar por catálogo."""
import pytest

from app.extraction.extractor import extract
from app.services.catalogs import holder_on, media_alias_map
from tests.test_documents import reference_doc, roles  # noqa: F401  (fixtures)
from tests.test_extraction import REFERENCE_PDF

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


async def test_initial_rule_set_is_unapproved_draft(roles):
    r = await roles["consulta"].get("/api/v1/rules")
    body = r.json()
    draft = body["draft"]
    if body["active"] is None:  # estado inicial
        assert draft["status"] == "draft" and draft["version"].endswith("-V0.3")
        assert len(draft["topics"]) == 32 and all(t["code"].startswith("T") for t in draft["topics"])
    triage = (body["active"] or draft)["triage"]
    assert triage["mode"] == "simulation"
    assert sum(f["weight"] for f in triage["factors"]) == 100
    assert [l["min_score"] for l in triage["levels"]] == [85, 70, 50, 30, 0]
    assert body["can_edit_topics"] is False


async def test_rule_permissions(roles):
    for role in ("consulta", "editor", "auditor"):
        assert (await roles[role].put("/api/v1/rules/draft/topics/T01", json={"name": "X"})).status_code == 403
        assert (await roles[role].put("/api/v1/rules/draft/triage", json={})).status_code == 403
        assert (await roles[role].post("/api/v1/rules/draft/approve")).status_code == 403


async def test_edit_validate_approve_and_new_version(roles):
    admin = roles["admin"]
    state = (await admin.get("/api/v1/rules")).json()
    if not state["draft"]:
        assert (await admin.post("/api/v1/rules/draft", json={"notes": "prueba"})).status_code == 200
    draft = (await admin.get("/api/v1/rules")).json()["draft"]

    t1 = next(t for t in draft["topics"] if t["code"] == "T01")
    r = await admin.put("/api/v1/rules/draft/topics/T01", json={**t1, "synonyms": t1["synonyms"] + ["delincuencia"], "code": "ZZ"})
    assert r.status_code == 200
    t1b = next(t for t in r.json()["draft"]["topics"] if t["code"] == "T01")  # el código no cambia
    assert "delincuencia" in t1b["synonyms"]

    r = await admin.post("/api/v1/rules/draft/topics", json={"name": "Tema de prueba", "synonyms": ["palabra de prueba"]})
    new = r.json()["draft"]["topics"][-1]
    assert new["code"] not in {t["code"] for t in draft["topics"]} and new["active"]
    assert (await admin.post("/api/v1/rules/draft/topics", json={"name": "Tema de prueba"})).status_code == 400
    r = await admin.put(f"/api/v1/rules/draft/topics/{new['code']}", json={**new, "active": False})
    assert next(t for t in r.json()["draft"]["topics"] if t["code"] == new["code"])["active"] is False

    triage = r.json()["draft"]["triage"]
    bad = {**triage, "factors": [{**triage["factors"][0], "weight": 30}] + triage["factors"][1:]}
    r = await admin.put("/api/v1/rules/draft/triage", json=bad)
    assert r.status_code == 400 and "100" in r.json()["error"]
    bad_levels = {**triage, "levels": [{"name": "A", "min_score": 50}, {"name": "B", "min_score": 50}]}
    assert (await admin.put("/api/v1/rules/draft/triage", json=bad_levels)).status_code == 400
    good = {**triage, "levels": [*triage["levels"][:-2], {"code": "bajo", "name": "Bajo", "min_score": 25},
                                 triage["levels"][-1]]}
    assert (await admin.put("/api/v1/rules/draft/triage", json=good)).status_code == 200

    r = await admin.post("/api/v1/rules/draft/approve")
    assert r.status_code == 200, r.text
    approved = r.json()["approved"]
    assert approved["status"] == "approved" and approved["approved_by"] == admin.user["id"]
    assert approved["triage"]["mode"] == "simulation"  # aprobar no activa el triage

    state = (await admin.get("/api/v1/rules")).json()
    assert state["active"]["version"] == approved["version"] and state["draft"] is None
    assert (await admin.put("/api/v1/rules/draft/topics/T01", json=t1)).status_code == 400  # sin borrador no se edita

    r = await admin.post("/api/v1/rules/draft", json={"notes": "siguiente"})
    nxt = r.json()["draft"]
    assert nxt["based_on"] == approved["version"] and nxt["version"] != approved["version"]
    assert (await admin.post("/api/v1/rules/draft", json={})).status_code == 400  # un solo borrador
    assert (await admin.delete("/api/v1/rules/draft")).status_code == 200

    logs = (await admin.get("/api/v1/audit/logs?limit=200")).json()["logs"]
    actions = {l["action"] for l in logs}
    assert {"TOPIC_UPDATED", "TOPIC_CREATED", "TRIAGE_CONFIG_UPDATED", "RULESET_APPROVED",
            "RULESET_DRAFT_CREATED", "RULESET_DRAFT_DISCARDED"} <= actions


async def test_topic_radar_uses_catalog(roles, reference_doc):
    r = await roles["consulta"].get("/api/v1/news/topics?date_from=2026-10-05&date_to=2026-10-05")
    body = r.json()
    assert body["rule_set_version"] and body["total_news"] >= 155
    assert body["topics_with_news"] > 0 and body["unclassified"] < body["total_news"]
    top = body["topics"][0]
    assert top["count"] > 0 and top["headlines"] and top["matched_terms"]
    assert "provisional" in body["method"]


async def test_media_catalog_and_extractor_aliases(roles):
    admin = roles["admin"]
    media = (await roles["consulta"].get("/api/v1/catalogs/media")).json()["media"]
    names = {m["name"] for m in media}
    assert {"Diario Libre", "Listín Diario", "Hoy", "El Día", "El Caribe", "El Nuevo Diario"} <= names
    assert (await roles["editor"].post("/api/v1/catalogs/media", json={"name": "X"})).status_code == 403
    r = await admin.post("/api/v1/catalogs/media", json={"name": "Otro Medio", "aliases": ["hoy"]})
    assert r.status_code == 400 and "Hoy" in r.json()["error"]  # alias ya usado por otro medio
    r = await admin.post("/api/v1/catalogs/media", json={"name": "Medio Prueba", "aliases": ["medio prueba (diario)"],
                                                         "domain": "medioprueba.com.do"})
    assert r.status_code == 201
    mid = r.json()["media"]["id"]
    r = await admin.put(f"/api/v1/catalogs/media/{mid}", json={"name": "Medio Prueba", "aliases": [], "status": "inactive"})
    assert r.json()["media"]["status"] == "inactive"
    aliases = await media_alias_map()
    assert aliases["el día (diario)"] == "El Día" and "medio prueba" not in aliases
    if REFERENCE_PDF.exists():
        res = extract(REFERENCE_PDF.read_bytes(), aliases)
        assert all(i.media_name for i in res.items)


async def test_officeholders(roles):
    admin = roles["admin"]
    assert (await roles["editor"].post("/api/v1/catalogs/officeholders", json={})).status_code == 403
    base = {"position": "Cargo de Prueba", "person_name": "Persona Uno", "state_power": "executive",
            "term_start": "2020-08-16", "term_end": "2024-08-15", "source": "Gaceta Oficial"}
    r = await admin.post("/api/v1/catalogs/officeholders", json=base)
    assert r.status_code == 201
    r = await admin.post("/api/v1/catalogs/officeholders", json={**base, "person_name": "Persona Dos",
                                                                 "term_start": "2024-01-01", "term_end": None})
    assert r.status_code == 400 and "solapa" in r.json()["error"]
    r = await admin.post("/api/v1/catalogs/officeholders", json={**base, "person_name": "Persona Dos",
                                                                 "term_start": "2024-08-16", "term_end": None})
    assert r.status_code == 201
    r = await admin.post("/api/v1/catalogs/officeholders", json={**base, "term_start": "2025-01-01", "term_end": "2024-01-01"})
    assert r.status_code == 400
    assert (await holder_on("cargo de prueba", "2022-05-01"))["person_name"] == "Persona Uno"
    assert (await holder_on("Cargo de Prueba", "2026-10-05"))["person_name"] == "Persona Dos"
