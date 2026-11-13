"""Fase 4: revisión humana con doble control, permisos por grupo de campos, bloqueo, muestreo e historial."""
import random
from datetime import timedelta

import pytest

from app.core.db import get_db, utcnow
from app.services.review import apply_sampling
from app.services.topics import recompute_news_topics
from tests.conftest import admin_session, user_session
from tests.test_documents import reference_doc, roles  # noqa: F401  (fixtures)

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


@pytest.fixture
async def team(client, roles):
    admin = roles["admin"]
    return {**roles, "editor2": await user_session(client, admin, "editor")}


async def _pending(reference_doc, index: int) -> dict:
    """Pone una noticia del documento de referencia en la bandeja (simula una señal de extracción)."""
    col = get_db()["news"]
    await col.update_one({"document_id": reference_doc, "news_index": index},
                         {"$set": {"review_status": "pending_review", "review_reasons": ["media_unresolved"],
                                   "review_assigned_to": None, "review_locked_until": None}})
    return await col.find_one({"document_id": reference_doc, "news_index": index})


async def test_queue_permissions_and_counts(team, reference_doc):
    n = await _pending(reference_doc, 10)
    assert (await team["consulta"].get("/api/v1/review/queue")).status_code == 403
    r = await team["editor"].get("/api/v1/review/queue?status=open")
    body = r.json()
    assert any(i["id"] == n["id"] for i in body["items"])
    assert body["counts"]["pending_review"] >= 1
    assert any(x["code"] == "media_unresolved" for x in body["reasons"])
    assert "title" in body["editable_fields"] and body["can_approve"]


async def test_take_lock_and_expiry(team, reference_doc):
    n = await _pending(reference_doc, 11)
    e1, e2 = team["editor"], team["editor2"]
    assert (await e1.post(f"/api/v1/review/news/{n['id']}/take")).status_code == 200
    r = await e2.post(f"/api/v1/review/news/{n['id']}/take")
    assert r.status_code == 409 and e1.user["email"] in r.json()["error"]
    # la toma vence: otro revisor puede tomarla
    await get_db()["news"].update_one({"id": n["id"]}, {"$set": {"review_locked_until": utcnow() - timedelta(minutes=1)}})
    assert (await e2.post(f"/api/v1/review/news/{n['id']}/take")).status_code == 200
    assert (await e1.post(f"/api/v1/review/news/{n['id']}/release")).status_code == 409
    assert (await e2.post(f"/api/v1/review/news/{n['id']}/release")).status_code == 200


async def test_double_control_correction(team, reference_doc):
    n = await _pending(reference_doc, 12)
    e1, e2 = team["editor"], team["editor2"]
    url = f"/api/v1/review/news/{n['id']}"
    # sin tomarla no se puede proponer
    assert (await e1.post(url + "/propose", json={"changes": {"title": "X"}, "justification": "Corrección de prueba"})).status_code == 409
    await e1.post(url + "/take")
    assert (await e1.post(url + "/propose", json={"changes": {"title": "NUEVO TITULAR."}, "justification": "corto"})).status_code == 400
    assert (await e1.post(url + "/propose", json={"changes": {"title": n["title"]}, "justification": "Justificación suficiente"})).status_code == 400
    assert (await e1.post(url + "/propose", json={"changes": {"media_name": "Medio Inexistente"}, "justification": "Justificación suficiente"})).status_code == 400
    assert (await e1.post(url + "/propose", json={"changes": {"simhash": "x"}, "justification": "Justificación suficiente"})).status_code == 400
    r = await e1.post(url + "/propose", json={
        "changes": {"title": "TITULAR CORREGIDO POR REVISIÓN.", "media_name": "Hoy", "publication_date": "2026-10-04"},
        "justification": "El titular estaba truncado y el medio es Hoy según la cabecera."})
    assert r.status_code == 200, r.text
    review_id = r.json()["review"]["id"]
    assert (await get_db()["news"].find_one({"id": n["id"]}))["review_status"] == "pending_approval"

    # doble control: quien propone no aprueba
    r = await e1.post(f"/api/v1/review/corrections/{review_id}/approve", json={})
    assert r.status_code == 403 and "Doble control" in r.json()["error"]
    assert (await team["consulta"].post(f"/api/v1/review/corrections/{review_id}/approve", json={})).status_code == 403
    r = await e2.post(f"/api/v1/review/corrections/{review_id}/approve", json={"note": "Verificado contra el PDF"})
    assert r.status_code == 200, r.text

    after = await get_db()["news"].find_one({"id": n["id"]})
    assert after["title"] == "TITULAR CORREGIDO POR REVISIÓN." and after["media_name"] == "Hoy"
    assert after["publication_date"] == "2026-10-04" and after["date_basis"] == "manual"
    assert after["review_status"] == "corrected" and after["is_corrected"]
    assert set(after["corrected_fields"]) == {"title", "media_name", "publication_date"}
    # el original se conserva
    assert after["original_extraction"]["title"] == n["title"] and after["original_extraction"]["media_name"] == n["media_name"]
    assert after["text_original"] == n["text_original"]
    # se ve en el explorador con el valor corregido
    r = await team["consulta"].get(f"/api/v1/news?document_id={reference_doc}&media=Hoy&limit=200")
    assert any(i["id"] == n["id"] for i in r.json()["items"])
    # una propuesta resuelta no se vuelve a resolver
    assert (await e2.post(f"/api/v1/review/corrections/{review_id}/approve", json={})).status_code == 409

    assert (await team["consulta"].get(url + "/history")).status_code == 403   # requiere review:history_read
    hist = (await e2.get(url + "/history")).json()["history"]
    assert hist[0]["status"] == "approved" and hist[0]["proposed_by"] == e1.user["id"] and hist[0]["decided_by"] == e2.user["id"]
    assert {c["field"] for c in hist[0]["changes"]} == {"title", "media_name", "publication_date"}


async def test_return_proposal(team, reference_doc):
    n = await _pending(reference_doc, 13)
    e1, e2 = team["editor"], team["editor2"]
    await e2.post(f"/api/v1/review/news/{n['id']}/take")
    r = await e2.post(f"/api/v1/review/news/{n['id']}/propose", json={"changes": {"author_raw": "Autor Supuesto"},
                                                                       "justification": "Autor visible en la cabecera."})
    review_id = r.json()["review"]["id"]
    assert (await e1.post(f"/api/v1/review/corrections/{review_id}/return", json={"note": ""})).status_code == 400
    assert (await e1.post(f"/api/v1/review/corrections/{review_id}/return", json={"note": "El autor no aparece en el PDF"})).status_code == 200
    after = await get_db()["news"].find_one({"id": n["id"]})
    assert after["review_status"] == "pending_review" and after.get("author_raw") == n.get("author_raw")


async def test_confirm_and_reject(team, reference_doc):
    e1 = team["editor"]
    a = await _pending(reference_doc, 14)
    await e1.post(f"/api/v1/review/news/{a['id']}/take")
    assert (await e1.post(f"/api/v1/review/news/{a['id']}/confirm", json={})).status_code == 200
    assert (await get_db()["news"].find_one({"id": a["id"]}))["review_status"] == "approved"

    b = await _pending(reference_doc, 15)
    await e1.post(f"/api/v1/review/news/{b['id']}/take")
    assert (await e1.post(f"/api/v1/review/news/{b['id']}/reject", json={"justification": "x"})).status_code == 400
    assert (await e1.post(f"/api/v1/review/news/{b['id']}/reject", json={"justification": "Es un recuadro de cifras, no una noticia."})).status_code == 200
    rejected = await get_db()["news"].find_one({"id": b["id"]})
    assert rejected["review_status"] == "rejected" and rejected["title"] == b["title"]  # se conserva


async def test_field_group_claims(team, reference_doc):
    admin, e1 = team["admin"], team["editor"]
    roles = (await admin.get("/api/v1/roles")).json()["role_claims"]
    editor_claims = roles["editor"]
    await admin.post("/api/v1/roles", json={"role": "editor", "claims": [c for c in editor_claims if c != "review:edit_text"]})
    try:
        n = await _pending(reference_doc, 16)
        await e1.post(f"/api/v1/review/news/{n['id']}/take")
        r = await e1.post(f"/api/v1/review/news/{n['id']}/propose", json={"changes": {"body": "Texto nuevo"},
                                                                           "justification": "Corrección del texto extraído."})
        assert r.status_code == 403 and "review:edit_text" in r.json()["error"]
        q = (await e1.get("/api/v1/review/queue")).json()
        assert "body" not in q["editable_fields"] and "title" in q["editable_fields"]
        await e1.post(f"/api/v1/review/news/{n['id']}/release")
    finally:
        await admin.post("/api/v1/roles", json={"role": "editor", "claims": editor_claims})


async def test_manual_topics_survive_recompute(team, reference_doc):
    e1, e2 = team["editor"], team["editor2"]
    n = await _pending(reference_doc, 17)
    await e1.post(f"/api/v1/review/news/{n['id']}/take")
    r = await e1.post(f"/api/v1/review/news/{n['id']}/propose", json={"changes": {"provisional_topics": ["T05", "T01"]},
                                                                       "justification": "Temas asignados por el revisor."})
    await e2.post(f"/api/v1/review/corrections/{r.json()['review']['id']}/approve", json={})
    await recompute_news_topics()
    after = await get_db()["news"].find_one({"id": n["id"]})
    assert after["provisional_topics"] == ["T01", "T05"] and after["topics_manual"] is True


async def test_sampling(team):
    rng = random.Random(42)
    sampled = sum(apply_sampling("not_required", [], 0.05, rng)[0] == "pending_review" for _ in range(10000))
    assert 400 < sampled < 600  # ~5 %
    assert apply_sampling("not_required", [], 0.0)[0] == "not_required"
    assert apply_sampling("pending_review", ["title_not_found"], 1.0) == ("pending_review", ["title_not_found"])
    assert (await team["editor"].put("/api/v1/review/settings", json={"sampling_rate": 0.1})).status_code == 403
    assert (await team["admin"].put("/api/v1/review/settings", json={"sampling_rate": 2})).status_code == 400
    assert (await team["admin"].put("/api/v1/review/settings", json={"sampling_rate": 0.1})).json()["sampling_rate"] == 0.1
    assert (await team["editor"].get("/api/v1/review/settings")).json()["sampling_rate"] == 0.1
    await team["admin"].put("/api/v1/review/settings", json={"sampling_rate": 0.05})


async def test_review_audit_trail(team):
    logs = (await team["admin"].get("/api/v1/audit/logs?limit=200")).json()["logs"]
    actions = {l["action"] for l in logs}
    assert {"REVIEW_TAKEN", "NEWS_CORRECTION_PROPOSED", "NEWS_CORRECTION_APPROVED", "NEWS_CORRECTION_RETURNED",
            "NEWS_ITEM_APPROVED", "NEWS_ITEM_DISCARDED", "REVIEW_SETTINGS_UPDATED"} <= actions
    assert (await team["admin"].get("/api/v1/audit/verify")).json()["integrity_status"] == "VERIFIED"
