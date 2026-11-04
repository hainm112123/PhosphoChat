"""Admin: choose the model for each role (chat, wiki jobs, fact-check) from live provider lists."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.settings import ModelSettings

from app import model_catalog
from app.agents.runtime import explain_error
from app.auth.deps import auth_store, require_admin
from app.auth.store import User
from app.llm import ROLES, check_model, get_model, role_models, role_sources

router = APIRouter(prefix="/api/models", dependencies=[Depends(require_admin)])

ROLE_HELP = {
    "chat": "Answers questions in Chat.",
    "job": "Capture, sync, lint and digest: writes the wiki.",
    "verify": "Fact-checks new pages against their source.",
}


class RoleChoices(BaseModel):
    chat: str | None = None    # None = leave as is; "" = reset to .env
    job: str | None = None
    verify: str | None = None


class TestIn(BaseModel):
    model: str


def _settings() -> dict:
    models, sources = role_models(), role_sources()
    return {"roles": {r: {"model": models[r], "source": sources[r], "help": ROLE_HELP[r],
                          "status": check_model(models[r])} for r in ROLES}}


@router.get("/catalog")
async def catalog(refresh: bool = False) -> dict:
    return await model_catalog.list_catalog(refresh)


@router.get("/settings")
def get_role_settings() -> dict:
    return _settings()


@router.put("/settings")
def put_role_settings(body: RoleChoices, admin: User = Depends(require_admin)) -> dict:
    store = auth_store()
    changes = {r: getattr(body, r) for r in ROLES if getattr(body, r) is not None}
    for role, value in changes.items():  # validate everything before saving anything
        value = value.strip()
        if value:
            st = check_model(value)
            if st["status"] in ("invalid", "missing"):
                raise HTTPException(400, f"{role}: {st['detail']}")
    for role, value in changes.items():
        if value.strip():
            store.set_setting(f"model.{role}", value.strip(), admin.id)
        else:
            store.delete_setting(f"model.{role}")
    return _settings()


@router.post("/test")
async def test_model(body: TestIn) -> dict:
    """One tiny request, to check a model works before choosing it."""
    t = time.monotonic()
    try:
        agent = Agent(get_model(body.model), instructions="Reply with the single word OK.")
        result = await asyncio.wait_for(
            agent.run("Reply with OK.", usage_limits=UsageLimits(request_limit=1),
                      model_settings=ModelSettings(max_tokens=512, temperature=0)),
            timeout=90)
        return {"ok": True, "reply": str(result.output).strip()[:200],
                "seconds": round(time.monotonic() - t, 1)}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "No answer within 90 seconds.", "seconds": 90}
    except Exception as e:  # noqa: BLE001 - shown on the Models page
        return {"ok": False, "error": explain_error(e), "seconds": round(time.monotonic() - t, 1)}
