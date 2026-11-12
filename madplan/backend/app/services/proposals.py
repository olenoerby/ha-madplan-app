"""What the assistant may offer to do. It only offers: a person approves, and it runs as them.

An offer is stored with the answer, and an approval refers to that stored offer, so the browser
cannot change what gets done. Text from offers and web pages can steer a model, so an offer has to
be harmless to show and has to be spelled out where the person presses the button. Everything
goes through the same rules as if the person had done it by hand, and what someone must not eat
is removed both when it is offered and when it is approved.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChatMessage, Household, PriceAlert, User
from app.services.alerts import evaluate_alert
from app.services.allergens import conflicting_allergy
from app.services.planner import NoRecipes, PlanRequest, generate_plan, notify_plan_ready, resolve_dates
from app.services.recipes import household_profile
from app.services.shopping import add_manual_item
from app.settings_schema import household_settings

MAX_PER_ANSWER = 3
_MAX_ITEMS = 30
_ALERT_BASES = ("pack", "kg", "l", "stk")
_PLAN_MODES = ("full_week", "workweek", "days_ahead")


class ProposalError(ValueError):
    """The offer no longer makes sense, or was already answered."""


def _safe_items(db: Session, household: Household, items: list[str]) -> list[str]:
    profile = household_profile(db, household)
    seen: dict[str, str] = {}
    for raw in items:
        name = (raw or "").strip()[:200]
        if name and not conflicting_allergy(name, profile.allergies):
            seen.setdefault(name.lower(), name)
    return list(seen.values())[:_MAX_ITEMS]


def shopping_items(db: Session, household: Household, items: list[str]) -> dict | None:
    safe = _safe_items(db, household, items)
    return {"type": "add_to_shopping_list", "items": safe, "status": "open"} if safe else None


def price_alert(db: Session, household: Household, query: str, max_price: float, basis: str = "pack") -> dict | None:
    cleaned = (query or "").strip()[:160]
    profile = household_profile(db, household)
    if len(cleaned) < 2 or max_price <= 0 or basis not in _ALERT_BASES or conflicting_allergy(cleaned, profile.allergies):
        return None
    return {"type": "create_price_alert", "query": cleaned, "max_price": round(float(max_price), 2), "basis": basis, "status": "open"}


def meal_plan(mode: str = "full_week", budget: float | None = None, days_ahead: int = 7) -> dict | None:
    if mode not in _PLAN_MODES or (budget is not None and budget <= 0):
        return None
    return {
        "type": "make_plan",
        "mode": mode,
        "days_ahead": max(1, min(int(days_ahead), 31)),
        "budget": round(float(budget), 2) if budget is not None else None,
        "status": "open",
    }


def _carry_out(db: Session, user: User, household: Household, proposal: dict) -> dict:
    kind = proposal.get("type")
    if kind == "add_to_shopping_list":
        safe = _safe_items(db, household, proposal.get("items") or [])
        for name in safe:
            add_manual_item(db, household.id, name)
        return {"added": safe}
    if kind == "create_price_alert":
        alert = PriceAlert(user_id=user.id, query=proposal["query"], max_price=proposal["max_price"], basis=proposal["basis"])
        db.add(alert)
        db.flush()
        evaluate_alert(db, alert, user)
        return {"alert_id": alert.id}
    if kind == "make_plan":
        dates = resolve_dates(proposal["mode"], days_ahead=proposal.get("days_ahead") or 7)
        meal_types = list(household_settings(household.settings).default_meal_types)
        try:
            plan = generate_plan(db, household, PlanRequest(dates=dates, meal_types=meal_types, budget=proposal.get("budget")), user)
        except NoRecipes as exc:
            raise ProposalError("no_recipes") from exc
        notify_plan_ready(db, plan, user)
        return {"plan_id": plan.id}
    raise ProposalError("unknown")


def answer(db: Session, user: User, message_id: int, index: int, approve: bool) -> dict:
    """Approves or declines one offer. Only the person it was made to can answer it, once."""
    message = db.scalar(select(ChatMessage).where(ChatMessage.id == message_id, ChatMessage.user_id == user.id))
    proposals = list((message.proposals if message else None) or [])
    if not 0 <= index < len(proposals):
        raise LookupError("not_found")
    proposal = dict(proposals[index])
    if proposal.get("status") != "open":
        raise ProposalError("already_answered")
    if approve:
        proposal["result"] = _carry_out(db, user, db.get(Household, user.household_id), proposal)
        proposal["status"] = "done"
    else:
        proposal["status"] = "declined"
    proposals[index] = proposal
    # A new list, as a change inside a JSON column is not noticed by itself.
    message.proposals = proposals
    db.flush()
    return proposal
