"""Administration inside the app: people's roles, a check of the setup, and crawling.

What an administrator chooses is set in the app's configuration in Home Assistant, not here:
this page only shows it, and tells whether it works.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import AdminUser, Db
from app.core import ha
from app.crawlers.sources import SOURCES, all_source_infos
from app.integrations.mealie import MealieError
from app.models import AppSetting, CrawlRun, CrawlSource, Offer, User
from app.services.app_settings import get_schedule, mealie_client
from app.services.mealie_sync import remember_group

router = APIRouter(prefix="/admin", tags=["admin"])


def _user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "is_active": user.is_active,
        "last_login_at": user.last_login_at,
        # Administrators are Home Assistant's, and are changed there.
        "role_from_home_assistant": user.role == "admin",
    }


@router.get("/users")
def list_users(admin: AdminUser, db: Db) -> list[dict]:
    """Everyone who has opened MadPlan from Home Assistant."""
    return [_user_payload(u) for u in db.scalars(select(User).where(User.ha_user_id.is_not(None)).order_by(User.display_name))]


class RoleIn(BaseModel):
    role: Literal["household_manager", "user", "guest"]


@router.patch("/users/{user_id}")
def set_role(user_id: int, payload: RoleIn, admin: AdminUser, db: Db) -> dict:
    """MadPlan's own roles for those who are not administrators in Home Assistant."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    if user.role == "admin":
        raise HTTPException(status.HTTP_409_CONFLICT, "admin_follows_home_assistant")
    user.role = payload.role
    db.commit()
    return _user_payload(user)


@router.get("/setup")
def setup(admin: AdminUser, db: Db) -> dict:
    """The app's configuration as it is now, with secrets left out, and the AI agents to pick from."""
    options = ha.options()
    schedule = get_schedule(db)
    entities = ha.ai_task_entities()
    return {
        "mealie": {
            "configured": options.mealie_configured,
            "url": options.mealie_url,
            "public_url": options.mealie_public_url,
            "token_set": bool(options.mealie_token),
            "shopping_list": options.mealie_shopping_list,
            "sync_minutes": options.mealie_sync_minutes,
        },
        "ai": {
            "entity": options.ai_task_entity,
            "configured": options.ai_configured,
            "found": any(e["entity_id"] == options.ai_task_entity for e in entities) if options.ai_task_entity else None,
            "available": entities,
        },
        "notify_service": options.notify_service,
        "assistant_reads_states": options.assistant_reads_states,
        "language": ha.ha_language(),
        "schedule": schedule.model_dump(mode="json"),
    }


@router.post("/setup/test")
def test_setup(admin: AdminUser, db: Db) -> dict:
    results: dict[str, dict] = {}
    client = mealie_client(db, admin.household_id)
    if client is None:
        results["mealie"] = {"ok": False, "error": "not_configured"}
    else:
        try:
            with client:
                info = client.about()
                remember_group(db, admin.household_id, client)
                db.commit()
            results["mealie"] = {"ok": True, "version": info.get("version")}
        except MealieError as exc:
            results["mealie"] = {"ok": False, "error": str(exc)[:300]}
    try:
        config = ha.core_request("GET", "/api/config")
        results["home_assistant"] = {"ok": True, "version": (config or {}).get("version")}
    except ha.HomeAssistantUnavailable as exc:
        results["home_assistant"] = {"ok": False, "error": str(exc)[:300]}
    return results


# Crawling


@router.get("/sources")
def list_sources(admin: AdminUser, db: Db) -> list[dict]:
    states = {s.slug: s for s in db.scalars(select(CrawlSource))}
    counts = dict(db.execute(select(Offer.source, func.count(Offer.id)).group_by(Offer.source)).all())
    results = []
    for info in all_source_infos():
        state = states.get(info.slug)
        results.append(
            {
                "slug": info.slug,
                "name": info.name,
                "homepage": info.homepage,
                "policy": info.status,
                "note": info.note,
                "terms_checked_on": info.terms_checked_on,
                "enabled": bool(state and state.enabled) and info.status == "allowed",
                "can_enable": info.status == "allowed",
                "last_run_at": state.last_run_at if state else None,
                "last_status": state.last_status if state else None,
                "offers": counts.get(info.slug, 0),
            }
        )
    return results


class SourceIn(BaseModel):
    enabled: bool


@router.patch("/sources/{slug}")
def update_source(slug: str, payload: SourceIn, admin: AdminUser, db: Db) -> dict:
    if slug not in SOURCES:
        raise HTTPException(status.HTTP_409_CONFLICT, "source_blocked_or_unknown")
    state = db.get(CrawlSource, slug)
    if state is None:
        state = CrawlSource(slug=slug)
        db.add(state)
    state.enabled = payload.enabled
    db.commit()
    return {"slug": slug, "enabled": state.enabled}


def request_crawl(db) -> None:
    row = db.get(AppSetting, "worker_requests")
    if row is None:
        db.add(AppSetting(key="worker_requests", value={"crawl": True}))
    else:
        row.value = {**(row.value or {}), "crawl": True}


@router.post("/crawl", status_code=202)
def crawl_now(admin: AdminUser, db: Db) -> dict:
    """The background jobs do the crawling, as it takes minutes at a polite pace."""
    request_crawl(db)
    db.commit()
    return {"queued": True}


@router.get("/crawl/runs")
def crawl_runs(admin: AdminUser, db: Db, limit: int = 20) -> list[dict]:
    rows = db.scalars(select(CrawlRun).order_by(CrawlRun.id.desc()).limit(min(limit, 100)))
    return [
        {
            "id": r.id,
            "source": r.source,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "status": r.status,
            "terms": r.terms,
            "requests": r.requests,
            "denied_by_policy": r.denied_by_policy,
            "offers_seen": r.offers_seen,
            "error": r.error,
        }
        for r in rows
    ]
