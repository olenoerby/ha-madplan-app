"""The API the Home Assistant integration polls. Authenticated with a personal API token."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, Db, household_of
from app.core.db import local_today, utcnow
from app.models import MealPlan, MealVote, Notification, PantryItem, PlannedMeal, PriceAlert, ShoppingItem
from app.services.budget import dashboard
from app.services.household_care import expiring_items
from app.services.leftovers import FollowsAnotherMeal
from app.services.mealie_sync import MealieOff, send_household, unsent_accepted
from app.services.pantry import set_cooked
from app.services.price_index import price_index
from app.services.restock import restock_suggestions
from app.services.shopping import add_manual_item, build_shopping_list, open_list
from app.services.voting import MealBought, can_vote, cast_vote
from app.settings_schema import household_settings, user_settings

router = APIRouter(prefix="/ha", tags=["home-assistant"])

_MEAL_ORDER = {"breakfast": 0, "lunch": 1, "snack": 2, "dinner": 3, "drink": 4}


def _active_meals(db, household_id: int, first: date, last: date) -> list[PlannedMeal]:
    rows = db.scalars(
        select(PlannedMeal)
        .join(MealPlan, MealPlan.id == PlannedMeal.plan_id)
        .where(
            MealPlan.household_id == household_id,
            PlannedMeal.date >= first,
            PlannedMeal.date <= last,
            PlannedMeal.status != "rejected",
        )
    )
    return sorted(rows, key=lambda m: (m.date, _MEAL_ORDER.get(m.meal_type, 9)))


def _meals_to_vote(db, user, today: date) -> list[dict]:
    """What this person has not given an answer to yet, so a phone can ask them."""
    if not user_settings(user.settings).features.voting:
        return []
    voted = set(db.scalars(select(MealVote.meal_id).where(MealVote.user_id == user.id)))
    return [
        {"id": m.id, "date": m.date.isoformat(), "meal_type": m.meal_type, "title": m.title, "bought": m.bought}
        for m in _active_meals(db, user.household_id, today, today + timedelta(days=21))
        if m.status == "pending" and m.id not in voted and m.leftover_of is None
    ]


def _buy_signals(db, user, today: date) -> list[dict]:
    """Recent "buy now" messages for offers still running. Working them out on every poll would
    be wasteful, and the worker has done it already."""
    rows = db.scalars(
        select(Notification)
        .where(Notification.user_id == user.id, Notification.kind == "buy_now", Notification.created_at >= utcnow() - timedelta(days=7))
        .order_by(Notification.id.desc())
        .limit(30)
    )
    latest: dict[str, dict] = {}
    for row in rows:
        valid_to = row.data.get("valid_to")
        if valid_to and date.fromisoformat(valid_to) < today:
            continue
        latest.setdefault(row.data.get("query", ""), row.data)
    return list(latest.values())


@router.get("/summary")
def summary(user: CurrentUser, db: Db) -> dict:
    today = local_today()
    household = household_of(db, user)
    hs = household_settings(household.settings)
    upcoming = _active_meals(db, household.id, today, today + timedelta(days=7))
    todays = [m for m in upcoming if m.date == today]
    dinner = next((m for m in todays if m.meal_type == "dinner"), None)
    expiring = expiring_items(db, household.id, hs.waste_alert_days, today)
    shopping = open_list(db, household.id)
    remaining = [i for i in (shopping.items if shopping else []) if not i.checked and not i.covered_by_pantry]
    budget = dashboard(db, household, user, today)
    triggered = db.scalars(
        select(PriceAlert).where(PriceAlert.user_id == user.id, PriceAlert.active.is_(True), PriceAlert.last_triggered_at.is_not(None))
    ).all()
    unread = db.scalars(select(Notification.id).where(Notification.user_id == user.id, Notification.read_at.is_(None))).all()

    return {
        "household": household.name,
        "today_dinner": dinner.title if dinner else None,
        "today_meals": [{"meal_type": m.meal_type, "title": m.title, "status": m.status} for m in todays],
        "next_meal": (
            {"date": upcoming[0].date.isoformat(), "meal_type": upcoming[0].meal_type, "title": upcoming[0].title}
            if upcoming else None
        ),
        "shopping_remaining": len(remaining),
        "shopping_cost": round(sum(i.cost or 0 for i in remaining), 2),
        "shopping_list_id": shopping.id if shopping else None,
        "expiring_count": len(expiring),
        "expiring": [{"name": i.name, "expires_on": i.expires_on.isoformat(), "estimated": i.expires_estimated} for i in expiring],
        "running_low": _running_low(db, user, household, today),
        "buy_now": _buy_signals(db, user, today),
        "meals_to_vote": _meals_to_vote(db, user, today),
        "mealie_unsent": _mealie_unsent(db, household.id, today),
        "budget_week": budget["week"],
        "budget_month": budget["month"],
        "savings_year": budget["economy"]["saved_this_year"],
        "alerts": [
            {"query": a.query, "max_price": a.max_price, **(a.last_match or {})}
            for a in triggered
        ],
        "unread_notifications": len(unread),
    }


def _mealie_unsent(db, household_id: int, today: date) -> list[dict]:
    """Accepted meals Mealie does not have yet, so a phone can offer to send them."""
    return [
        {"id": m.id, "date": m.date.isoformat(), "meal_type": m.meal_type, "title": m.title}
        for m in unsent_accepted(db, household_id, today)
    ]


@router.post("/send-to-mealie")
def send_to_mealie(user: CurrentUser, db: Db) -> dict:
    """The "Save and send to Mealie" button, pressed from a phone, for all the household's plans."""
    try:
        queued = send_household(db, user.household_id, user)
    except MealieOff as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    db.commit()
    return {"queued": queued}


def _running_low(db, user, household, today: date) -> list[dict]:
    if not user_settings(user.settings).features.restock:
        return []
    return [
        {"name": s["name"], "reason": s["reason"], "quantity": s.get("quantity"), "unit": s.get("unit")}
        for s in restock_suggestions(db, household, user, today, with_offers=False)
    ]


@router.get("/calendar")
def calendar(user: CurrentUser, db: Db, start: date | None = None, end: date | None = None) -> list[dict]:
    first = start or local_today()
    last = end or first + timedelta(days=14)
    return [
        {"id": m.id, "date": m.date.isoformat(), "meal_type": m.meal_type, "title": m.title, "status": m.status}
        for m in _active_meals(db, user.household_id, first, last)
    ]


@router.get("/shopping")
def shopping(user: CurrentUser, db: Db) -> dict:
    current = open_list(db, user.household_id)
    if current is None:
        return {"id": None, "items": []}
    return {
        "id": current.id,
        "items": [
            {
                "id": i.id,
                "name": i.name,
                "quantity": i.quantity,
                "unit": i.unit,
                "checked": i.checked,
                "store": i.store.name if i.store else None,
                "cost": i.cost,
            }
            for i in current.items
            if not i.covered_by_pantry
        ],
    }


class ToggleIn(BaseModel):
    checked: bool


@router.post("/shopping/{item_id}")
def toggle_item(item_id: int, payload: ToggleIn, user: CurrentUser, db: Db) -> dict:
    item = db.get(ShoppingItem, item_id)
    if item is None or item.shopping_list.household_id != user.household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    item.checked = payload.checked
    db.commit()
    return {"id": item.id, "checked": item.checked}


class AddShoppingIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


@router.post("/shopping", status_code=201)
def add_item(payload: AddShoppingIn, user: CurrentUser, db: Db) -> dict:
    """For automations such as "buy more toilet paper"."""
    item = add_manual_item(db, user.household_id, payload.name, list_name="Home Assistant")
    db.commit()
    return {"id": item.id}


class PantryUpdateIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    quantity: float | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=12)
    expires_on: date | None = None
    min_quantity: float | None = Field(default=None, ge=0)


@router.post("/pantry")
def update_pantry(payload: PantryUpdateIn, user: CurrentUser, db: Db) -> dict:
    """Lets a sensor or a barcode scanner in Home Assistant report stock."""
    item = db.scalar(select(PantryItem).where(PantryItem.household_id == user.household_id, PantryItem.name == payload.name))
    if item is None:
        item = PantryItem(household_id=user.household_id, name=payload.name, source="ha")
        db.add(item)
    item.quantity, item.unit = payload.quantity, payload.unit
    if payload.expires_on:
        item.expires_on, item.expires_estimated = payload.expires_on, False
    if payload.min_quantity is not None:
        item.min_quantity = payload.min_quantity
    db.commit()
    return {"id": item.id}


def _household_meal(db, meal_id: int, user) -> PlannedMeal:
    meal = db.get(PlannedMeal, meal_id)
    if meal is None or meal.plan.household_id != user.household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    return meal


class VoteIn(BaseModel):
    vote: Literal["yes", "no"]


@router.post("/meals/{meal_id}/vote")
def vote_meal(meal_id: int, payload: VoteIn, user: CurrentUser, db: Db) -> dict:
    """Lets a button on a phone answer "shall we have this?"."""
    meal = _household_meal(db, meal_id, user)
    if not can_vote(db, meal.plan, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cannot_vote")
    try:
        cast_vote(db, meal, user, payload.vote)
    except MealBought as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "meal_bought") from exc
    except FollowsAnotherMeal as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "leftovers_follow") from exc
    db.commit()
    return {"id": meal.id, "status": meal.status}


class CookedIn(BaseModel):
    cooked: bool


@router.post("/meals/{meal_id}/cooked")
def meal_cooked(meal_id: int, payload: CookedIn, user: CurrentUser, db: Db) -> dict:
    meal = _household_meal(db, meal_id, user)
    if payload.cooked and meal.date > local_today():
        raise HTTPException(status.HTTP_409_CONFLICT, "not_yet")
    set_cooked(db, meal, payload.cooked)
    build_shopping_list(db, meal.plan, user)
    db.commit()
    return {"id": meal.id, "cooked": meal.cooked}


@router.get("/price/{query}")
def price(query: str, user: CurrentUser, db: Db) -> dict:
    return price_index(db, query, user, household_of(db, user))
