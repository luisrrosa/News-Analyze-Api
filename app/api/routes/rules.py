from fastapi import APIRouter, Depends, Request

from app.api.deps import ApiError, AuthContext, require
from app.core.audit import write_audit
import asyncio

from app.services import rules
from app.services.topics import recompute_news_topics

_background: set = set()


def _recompute_later() -> None:
    """Recalcula los temas de las noticias en segundo plano (no bloquea la respuesta)."""
    task = asyncio.create_task(recompute_news_topics())
    _background.add(task)
    task.add_done_callback(_background.discard)

router = APIRouter(prefix="/rules", tags=["rules"])


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _err(e: rules.RuleError) -> ApiError:
    return ApiError(400, str(e), "RULE_ERROR")


@router.get("")
async def overview(ctx: AuthContext = Depends(require("dashboard:read"))):
    """Versiones, vigente y borrador."""
    active, draft = await rules.get_active(), await rules.get_draft()
    return {"versions": await rules.list_versions(), "active": active, "draft": draft,
            "can_edit_topics": "topics:manage" in ctx.claims, "can_edit_triage": "triage:calibrate" in ctx.claims}


@router.get("/versions/{version}")
async def version_detail(version: str, ctx: AuthContext = Depends(require("dashboard:read"))):
    rs = await rules.get_version(version)
    if not rs:
        raise ApiError(404, "Versión no encontrada")
    return {"rule_set": rs}


@router.post("/draft")
async def new_draft(request: Request, ctx: AuthContext = Depends(require("topics:manage"))):
    body = await _body(request)
    try:
        draft = await rules.create_draft(ctx.user["id"], str(body.get("notes", "")))
    except rules.RuleError as e:
        raise _err(e)
    await write_audit(**ctx.actor(), action="RULESET_DRAFT_CREATED", entity_type="RULE_SET", entity_id=draft["version"],
                      status="SUCCESS", details={"based_on": draft["based_on"]})
    return {"draft": {k: v for k, v in draft.items() if k != "_id"}}


@router.delete("/draft")
async def delete_draft(ctx: AuthContext = Depends(require("topics:manage"))):
    try:
        version = await rules.discard_draft()
    except rules.RuleError as e:
        raise _err(e)
    await write_audit(**ctx.actor(), action="RULESET_DRAFT_DISCARDED", entity_type="RULE_SET", entity_id=version,
                      status="SUCCESS")
    return {"success": True}


@router.put("/draft/notes")
async def draft_notes(request: Request, ctx: AuthContext = Depends(require("topics:manage"))):
    try:
        draft = await rules.update_notes(str((await _body(request)).get("notes", "")), ctx.user["id"])
    except rules.RuleError as e:
        raise _err(e)
    return {"draft": draft}


@router.post("/draft/topics")
async def add_topic(request: Request, ctx: AuthContext = Depends(require("topics:manage"))):
    data = await _body(request)
    try:
        draft = await rules.save_topic(None, data, ctx.user["id"])
    except rules.RuleError as e:
        raise _err(e)
    await write_audit(**ctx.actor(), action="TOPIC_CREATED", entity_type="RULE_SET", entity_id=draft["version"],
                      status="SUCCESS", details={"topic": draft["topics"][-1]})
    _recompute_later()
    return {"draft": draft}


@router.put("/draft/topics/{code}")
async def edit_topic(code: str, request: Request, ctx: AuthContext = Depends(require("topics:manage"))):
    data = await _body(request)
    before = next((t for t in ((await rules.get_draft()) or {}).get("topics", []) if t["code"] == code), None)
    try:
        draft = await rules.save_topic(code, data, ctx.user["id"])
    except rules.RuleError as e:
        raise _err(e)
    after = next((t for t in draft["topics"] if t["code"] == code), None)
    await write_audit(**ctx.actor(), action="TOPIC_UPDATED", entity_type="RULE_SET", entity_id=draft["version"],
                      status="SUCCESS", details={"code": code, "before": before, "after": after})
    _recompute_later()
    return {"draft": draft}


@router.put("/draft/triage")
async def edit_triage(request: Request, ctx: AuthContext = Depends(require("triage:calibrate"))):
    before = ((await rules.get_draft()) or {}).get("triage")
    try:
        draft = await rules.save_triage(await _body(request), ctx.user["id"])
    except rules.RuleError as e:
        raise _err(e)
    await write_audit(**ctx.actor(), action="TRIAGE_CONFIG_UPDATED", entity_type="RULE_SET", entity_id=draft["version"],
                      status="SUCCESS", details={"before": before, "after": draft["triage"]})
    return {"draft": draft}


@router.post("/draft/approve")
async def approve(ctx: AuthContext = Depends(require("topics:manage", "triage:calibrate"))):
    try:
        approved, previous = await rules.approve_draft(ctx.user["id"])
    except rules.RuleError as e:
        raise _err(e)
    prev_doc = await rules.get_version(previous) if previous else None
    await write_audit(**ctx.actor(), action="RULESET_APPROVED", entity_type="RULE_SET", entity_id=approved["version"],
                      status="SUCCESS", details={"replaces": previous,
                                                 "changes": rules.diff_versions(prev_doc, approved) if prev_doc else None})
    _recompute_later()
    return {"approved": approved, "replaces": previous}
