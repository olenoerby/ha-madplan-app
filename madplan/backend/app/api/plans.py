"""Meal plans, votes, alternatives, the shopping list, recipes and the calendar."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.engine import AiUnavailable
from app.api.deps import CurrentUser, Db, household_of
from app.core import ha
from app.core.db import local_today
from app.models import (
    MealAlternative,
    MealPlan,
    Offer,
    PlannedMeal,
    PlanShare,
    Recipe,
    ShoppingItem,
    ShoppingList,
    User,
)
from app.services.app_settings import mealie_client
from app.services.catalog import section_of
from app.services.leftovers import FollowsAnotherMeal, leftovers_of
from app.services.manual_meals import (
    NotPlannable,
    SlotTaken,
    add_meal,
    also_planned,
    dishes_with_offer,
    move_meal,
    plan_leftovers,
    planning_view,
    set_bought,
    stop_leftovers,
)
from app.services.mealie_sync import MealieOff, calendar_entries, plan_state, send_plan
from app.services.new_recipes import find_new_recipes, household_links, recipe_link
from app.services.pantry import set_cooked
from app.services.planner import (
    NoRecipes,
    PlanRequest,
    fill_empty_slots,
    generate_plan,
    missing_slots,
    notify_plan_ready,
    notify_plan_shared,
    recipes_are_short,
    replace_meal,
    replacement_options,
    rescale_meals_for_day,
    resolve_dates,
    set_day_skipped,
)
from app.services.pricing import block_key
from app.services.receipts import proposal as receipt_proposal
from app.services.recipes import allergy_conflict, household_profile, meal_portions, visible_recipes
from app.services.shopping import build_shopping_list, complete_shopping_list
from app.services.text import normalize
from app.services.voting import MealBought, can_view_plan, can_vote, cast_vote, change_status, eligible_voters, ensure_can_drop
from app.settings_schema import MealType, household_settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["plans"])

# A long receipt can take a few photos. The same limits as photos of the pantry.
MAX_RECEIPT_PHOTOS = 4
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
RECEIPT_TYPES = {"image/jpeg", "image/png", "image/webp"}


class DayGuests(BaseModel):
    date: date
    adults: int = Field(default=0, ge=0, le=50)
    children: int = Field(default=0, ge=0, le=50)


class PlanIn(BaseModel):
    mode: Literal["days_ahead", "full_week", "workweek", "range"] = "full_week"
    days_ahead: int = Field(default=7, ge=1, le=31)
    start: date | None = None
    end: date | None = None
    meal_types: list[MealType] | None = None
    skip_dates: list[date] = []
    guests: list[DayGuests] = []
    adults: int | None = Field(default=None, ge=0, le=50)
    children: int | None = Field(default=None, ge=0, le=50)
    budget: float | None = Field(default=None, ge=0)
    use_ai: bool = True
    seed: int | None = None


def _get_plan(db: Session, plan_id: int, user: User) -> MealPlan:
    plan = db.get(MealPlan, plan_id)
    # 404 for someone else's plan too, so that ids cannot be probed.
    if plan is None or not can_view_plan(db, plan, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    return plan


def _get_meal(db: Session, meal_id: int, user: User) -> PlannedMeal:
    meal = db.get(PlannedMeal, meal_id)
    if meal is None or not can_view_plan(db, meal.plan, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    return meal


def _require_member(plan: MealPlan, user: User) -> None:
    """Sharing lets people outside the household look and vote, not rearrange the plan. A guest in
    the household is the same."""
    if plan.household_id != user.household_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "household_only")
    if user.is_guest:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "guest_read_only")


def _meal_payload(
    db: Session,
    meal: PlannedMeal,
    voters: int,
    links: tuple[str | None, str | None] = (None, None),
    nearby: list[dict] | None = None,
) -> dict:
    recipe = meal.recipe
    chooser = meal.added_by_user
    source = db.get(PlannedMeal, meal.leftover_of) if meal.leftover_of else None
    return {
        "id": meal.id,
        "date": meal.date,
        "meal_type": meal.meal_type,
        "title": meal.title,
        "title_en": recipe.title_en if recipe else None,
        "recipe_id": meal.recipe_id,
        "recipe_source": recipe.source if recipe else None,
        "adults": meal.adults,
        "children": meal.children,
        "status": meal.status,
        "bought": meal.bought,
        "added_by": {"id": chooser.id, "name": chooser.display_name} if chooser else None,
        "also_planned": nearby or [],
        "leftover_portions": meal.leftover_portions or 0,
        "leftover_of": {"id": source.id, "title": source.title, "date": source.date} if source else None,
        "leftovers": [{"id": m.id, "date": m.date, "meal_type": m.meal_type} for m in leftovers_of(db, meal)],
        "cooked": meal.cooked,
        "consumed": meal.consumed_at is not None,
        "estimated_cost": meal.estimated_cost,
        "normal_cost": meal.normal_cost,
        "saving": round(max(0.0, (meal.normal_cost or 0) - meal.estimated_cost), 2) if meal.normal_cost else 0,
        "reasons": meal.reasons,
        "sync_status": meal.sync_status,
        "sync_error": meal.sync_error,
        "mealie_slug": recipe.mealie_slug if recipe else None,
        "link": recipe_link(recipe, *links) if recipe else None,
        "voters": voters,
        "votes": [
            {"user_id": v.user_id, "name": v.user.display_name, "vote": v.vote, "comment": v.comment}
            for v in meal.votes
        ],
        "alternatives": [
            {
                "id": a.id,
                "title": a.title,
                "source": a.source,
                "recipe_id": a.recipe_id,
                "link": recipe_link(db.get(Recipe, a.recipe_id), *links) if a.recipe_id else None,
            }
            for a in meal.alternatives
        ],
    }


def _list_payload(shopping: ShoppingList | None) -> dict | None:
    if shopping is None:
        return None
    return {
        "id": shopping.id,
        "plan_id": shopping.plan_id,
        "name": shopping.name,
        "completed_at": shopping.completed_at,
        "summary": shopping.summary,
        "items": [
            {
                "id": i.id,
                "name": i.name,
                "quantity": i.quantity,
                "unit": i.unit,
                "packs": i.packs,
                "store": i.store.name if i.store else None,
                "offer_title": i.offer_title,
                "price_source": i.price_source,
                "cost": i.cost,
                "normal_cost": i.normal_cost,
                "normal_source": i.normal_source,
                "pack_quantity": i.pack_quantity,
                "saving": round(max(0.0, (i.normal_cost or 0) - (i.cost or 0)), 2) if i.normal_cost else 0,
                "covered_by_pantry": i.covered_by_pantry,
                "checked": i.checked,
                "manual": i.manual,
                "section": section_of(i.name),
            }
            for i in shopping.items
        ],
    }


def _plan_payload(db: Session, plan: MealPlan, user: User) -> dict:
    voters = len(eligible_voters(db, plan))
    shopping = db.scalar(select(ShoppingList).where(ShoppingList.plan_id == plan.id))
    member = plan.household_id == user.household_id
    # A guest sees the plan as its members do, but the page offers them only the vote.
    can_change = member and not user.is_guest
    # Someone a plan is shared with has no business with the address of this household's Mealie.
    links = household_links(db, plan.household_id) if member else (None, None)
    # Where else the household has the same dishes is the household's own business.
    nearby = also_planned(db, plan) if member else {}
    return {
        "id": plan.id,
        "start_date": plan.start_date,
        "end_date": plan.end_date,
        "meal_types": plan.meal_types,
        "budget": plan.budget,
        "ai_used": plan.ai_used,
        "created_at": plan.created_at,
        "is_member": can_change,
        "mealie": plan_state(db, plan) if member else None,
        "missing_slots": missing_slots(plan),
        "can_vote": can_vote(db, plan, user),
        "days": [
            {"date": d.date, "skipped": d.skipped, "guest_adults": d.guest_adults, "guest_children": d.guest_children}
            for d in plan.days
        ],
        "meals": [_meal_payload(db, m, voters, links, nearby.get(m.id)) for m in plan.meals],
        "shopping_list": _list_payload(shopping),
    }


def _dates_of(payload: PlanIn) -> list[date]:
    try:
        return resolve_dates(payload.mode, days_ahead=payload.days_ahead, start=payload.start, end=payload.end)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


def _mealie_in_period(db: Session, household_id: int, first: date, last: date) -> list[dict]:
    """What Mealie has on these days that no plan here knows of yet. Empty without Mealie."""
    if not ha.options().mealie_configured:
        return []
    plans = list(
        db.scalars(select(MealPlan).where(MealPlan.household_id == household_id, MealPlan.start_date <= last, MealPlan.end_date >= first))
    )
    # Notes about guests are MadPlan's own, whichever plan they came from.
    guest_notes = db.scalars(
        select(PlannedMeal.mealie_note_id)
        .join(MealPlan, MealPlan.id == PlannedMeal.plan_id)
        .where(MealPlan.household_id == household_id, PlannedMeal.mealie_note_id.is_not(None))
    )
    return calendar_entries(db, plans, household_id, first, last, ignore={str(n) for n in guest_notes})


def _take_from_mealie(db: Session, household_id: int) -> None:
    """Brings Mealie's own dishes onto the new plan now, rather than at the next reading."""
    from app.integrations.mealie import MealieError
    from app.services.mealie_pull import pull_plan

    client = mealie_client(db, household_id)
    if client is None:
        return
    try:
        with client:
            pull_plan(db, household_id, client)
    except MealieError as exc:
        # The next reading of Mealie brings them in.
        log.info("could not read Mealie's planner for a new plan: %s", exc)


class MealieNoteIn(BaseModel):
    date: date
    meal_type: MealType = "dinner"
    title: str = Field(min_length=1, max_length=120)


@router.post("/mealie/notes", status_code=201)
def add_mealie_note(payload: MealieNoteIn, user: CurrentUser, db: Db) -> dict:
    """A note in Mealie's planner, such as "eating out", written as the person's own."""
    from app.integrations.mealie import MealieError

    client = mealie_client(db, user.household_id)
    if client is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "mealie_not_configured")
    if payload.date < local_today():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "date_in_past")
    try:
        with client:
            entry = client.create_plan_entry(payload.date, payload.meal_type, title=payload.title.strip())
    except MealieError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "mealie_failed") from exc
    return {"id": str(entry.get("id")), "kind": "note", "date": payload.date, "meal_type": payload.meal_type, "title": payload.title.strip(), "from_madplan": False}


@router.post("/plans/preview")
def preview_plan(payload: PlanIn, user: CurrentUser, db: Db) -> dict:
    """The days a new plan would cover, and what Mealie has on them, before anything is made."""
    household = household_of(db, user)
    dates = _dates_of(payload)
    skip_weekdays = set(household_settings(household.settings).skip_weekdays)
    entries = _mealie_in_period(db, household.id, min(dates), max(dates))
    return {
        "start": min(dates),
        "end": max(dates),
        "days": [{"date": d, "always_skipped": d.weekday() in skip_weekdays} for d in dates],
        "mealie": {"connected": ha.options().mealie_configured, "entries": entries},
    }


@router.post("/plans", status_code=201)
def create_plan(payload: PlanIn, user: CurrentUser, db: Db) -> dict:
    household = household_of(db, user)
    hs = household_settings(household.settings)
    dates = _dates_of(payload)
    meal_types = list(payload.meal_types or hs.default_meal_types)
    if not meal_types:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no_meal_types")

    extra: list[Recipe] = []
    ai_note = None
    if payload.use_ai and recipes_are_short(db, household, meal_types, len(dates)):
        from app.services.app_settings import get_ai_settings

        count = get_ai_settings(db).plan_ai_candidates
        if count:
            try:
                extra = find_new_recipes(db, household, user, meal_types, count, max(min(dates), local_today()), user.locale).saved
            except AiUnavailable as exc:
                # The plan is made either way, and the page can say that no search took place.
                ai_note = exc.reason

    # A dish already in Mealie's planner keeps its place: Mealie has the last word.
    in_mealie = _mealie_in_period(db, household.id, min(dates), max(dates))
    taken = {
        (date.fromisoformat(str(e["date"])[:10]), e["meal_type"])
        for e in in_mealie
        if e["kind"] == "recipe" and not e["from_madplan"] and e["date"]
    }
    request = PlanRequest(
        dates=dates,
        meal_types=meal_types,
        skip_dates=set(payload.skip_dates),
        taken=taken,
        guests={g.date: (g.adults, g.children) for g in payload.guests},
        adults=payload.adults,
        children=payload.children,
        budget=payload.budget,
        seed=payload.seed,
    )
    try:
        plan = generate_plan(db, household, request, user, extra)
    except NoRecipes as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "no_recipes") from exc
    if in_mealie:
        _take_from_mealie(db, household.id)
    notify_plan_ready(db, plan, user)
    db.commit()
    result = _plan_payload(db, plan, user)
    result["ai_note"] = ai_note
    return result


@router.get("/plans")
def list_plans(user: CurrentUser, db: Db) -> list[dict]:
    own = db.scalars(select(MealPlan).where(MealPlan.household_id == user.household_id).order_by(MealPlan.start_date.desc()).limit(30))
    shared = db.scalars(select(MealPlan).join(PlanShare, PlanShare.plan_id == MealPlan.id).where(PlanShare.user_id == user.id))
    rows = list(own) + list(shared)
    results = []
    for plan in rows:
        shopping = db.scalar(select(ShoppingList).where(ShoppingList.plan_id == plan.id))
        results.append(
            {
                "id": plan.id,
                "start_date": plan.start_date,
                "end_date": plan.end_date,
                "meals": len(plan.meals),
                "accepted": sum(1 for m in plan.meals if m.status == "accepted"),
                "total": (shopping.summary or {}).get("total") if shopping else None,
                "shared": plan.household_id != user.household_id,
            }
        )
    return results


@router.get("/plans/current")
def current_plan(user: CurrentUser, db: Db) -> dict | None:
    plan = db.scalar(
        select(MealPlan)
        .where(MealPlan.household_id == user.household_id, MealPlan.end_date >= local_today())
        .order_by(MealPlan.start_date, MealPlan.id.desc())
        .limit(1)
    )
    return _plan_payload(db, plan, user) if plan else None


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int, user: CurrentUser, db: Db) -> dict:
    return _plan_payload(db, _get_plan(db, plan_id, user), user)


@router.delete("/plans/{plan_id}")
def delete_plan(plan_id: int, user: CurrentUser, db: Db) -> dict:
    plan = _get_plan(db, plan_id, user)
    _require_member(plan, user)
    db.delete(plan)
    db.commit()
    return {"ok": True}


class DayIn(BaseModel):
    skipped: bool | None = None
    guest_adults: int | None = Field(default=None, ge=0, le=50)
    guest_children: int | None = Field(default=None, ge=0, le=50)


@router.patch("/plans/{plan_id}/days/{day}")
def update_day(plan_id: int, day: date, payload: DayIn, user: CurrentUser, db: Db) -> dict:
    """Skip a day, or say there are guests. Amounts are redone, and cheaper dishes suggested."""
    plan = _get_plan(db, plan_id, user)
    _require_member(plan, user)
    suggestions: list[dict] = []
    try:
        if payload.skipped is not None:
            set_day_skipped(db, plan, day, payload.skipped, user)
        if payload.guest_adults is not None or payload.guest_children is not None:
            row = next((d for d in plan.days if d.date == day), None)
            if row is None:
                raise ValueError("date")
            suggestions = rescale_meals_for_day(
                db, plan, day,
                payload.guest_adults if payload.guest_adults is not None else row.guest_adults,
                payload.guest_children if payload.guest_children is not None else row.guest_children,
                user,
            )
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "date_not_in_plan") from exc
    except MealBought as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "meal_bought") from exc
    db.commit()
    return {"plan": _plan_payload(db, plan, user), "suggestions": suggestions}


class ShareIn(BaseModel):
    user_ids: list[int]
    can_vote: bool = True


@router.post("/plans/{plan_id}/share")
def share_plan(plan_id: int, payload: ShareIn, user: CurrentUser, db: Db) -> dict:
    plan = _get_plan(db, plan_id, user)
    _require_member(plan, user)
    for user_id in payload.user_ids:
        person = db.get(User, user_id)
        if person is None:
            continue
        row = db.get(PlanShare, (plan.id, user_id))
        if row is None:
            db.add(PlanShare(plan_id=plan.id, user_id=user_id, can_vote=payload.can_vote))
            notify_plan_shared(db, plan, person)
        else:
            row.can_vote = payload.can_vote
    db.commit()
    return {"shared_with": [s.user_id for s in db.scalars(select(PlanShare).where(PlanShare.plan_id == plan.id))]}


@router.get("/plans/{plan_id}/share-targets")
def share_targets(plan_id: int, user: CurrentUser, db: Db) -> list[dict]:
    plan = _get_plan(db, plan_id, user)
    _require_member(plan, user)
    rows = db.scalars(select(User).where(User.is_active.is_(True), User.id != user.id))
    existing = {s.user_id for s in db.scalars(select(PlanShare).where(PlanShare.plan_id == plan.id))}
    return [
        {"id": u.id, "display_name": u.display_name, "shared": u.id in existing, "same_household": u.household_id == plan.household_id}
        for u in rows
    ]


@router.post("/plans/{plan_id}/send-to-mealie")
def send_to_mealie(plan_id: int, user: CurrentUser, db: Db) -> dict:
    """The "Save and send to Mealie" button. Until someone presses it, nothing from the plan goes there."""
    plan = _get_plan(db, plan_id, user)
    _require_member(plan, user)
    try:
        send_plan(db, plan)
    except MealieOff as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    db.commit()
    return _plan_payload(db, plan, user)


@router.get("/plans/{plan_id}/mealie-entries")
def mealie_entries(plan_id: int, user: CurrentUser, db: Db) -> list[dict]:
    """What Mealie has planned on these days already, whatever put it there, apart from this plan."""
    plan = _get_plan(db, plan_id, user)
    _require_member(plan, user)
    # Notes about guests are not dishes, whichever plan they came from.
    notes = db.scalars(
        select(PlannedMeal.mealie_note_id)
        .join(MealPlan, MealPlan.id == PlannedMeal.plan_id)
        .where(MealPlan.household_id == plan.household_id, PlannedMeal.mealie_note_id.is_not(None))
    )
    return calendar_entries(db, [plan], plan.household_id, plan.start_date, plan.end_date, ignore=set(notes))


@router.post("/plans/{plan_id}/accept-all")
def accept_all(plan_id: int, user: CurrentUser, db: Db) -> dict:
    plan = _get_plan(db, plan_id, user)
    _require_member(plan, user)
    for meal in plan.meals:
        # Leftovers follow the meal they come from.
        if meal.status == "pending" and meal.leftover_of is None:
            cast_vote(db, meal, user, "yes")
    db.commit()
    return _plan_payload(db, plan, user)


class VoteIn(BaseModel):
    vote: Literal["yes", "no"]
    comment: str | None = Field(default=None, max_length=300)


@router.post("/meals/{meal_id}/vote")
def vote(meal_id: int, payload: VoteIn, user: CurrentUser, db: Db) -> dict:
    meal = _get_meal(db, meal_id, user)
    if not can_vote(db, meal.plan, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cannot_vote")
    try:
        cast_vote(db, meal, user, payload.vote, payload.comment)
    except MealBought as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "meal_bought") from exc
    except FollowsAnotherMeal as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "leftovers_follow") from exc
    db.commit()
    return _plan_payload(db, meal.plan, user)


class CookedIn(BaseModel):
    cooked: bool


@router.post("/meals/{meal_id}/cooked")
def cooked(meal_id: int, payload: CookedIn, user: CurrentUser, db: Db) -> dict:
    """Whether a meal was made. Yes takes its food out of the pantry now, no puts it back."""
    meal = _get_meal(db, meal_id, user)
    _require_member(meal.plan, user)
    if payload.cooked and meal.date > local_today():
        raise HTTPException(status.HTTP_409_CONFLICT, "not_yet")
    set_cooked(db, meal, payload.cooked)
    build_shopping_list(db, meal.plan, user)
    db.commit()
    return _plan_payload(db, meal.plan, user)


class PeopleIn(BaseModel):
    adults: int = Field(ge=0, le=50)
    children: int = Field(ge=0, le=50)


@router.patch("/meals/{meal_id}/people")
def set_people(meal_id: int, payload: PeopleIn, user: CurrentUser, db: Db) -> dict:
    """How many are eating this particular meal."""
    meal = _get_meal(db, meal_id, user)
    _require_member(meal.plan, user)
    household = household_of(db, user)
    hs = household_settings(household.settings)
    old = meal_portions(meal, hs.child_portion)
    meal.adults, meal.children = payload.adults, payload.children
    new = meal_portions(meal, hs.child_portion)
    meal.estimated_cost = round(meal.estimated_cost * new / old, 2)
    if meal.normal_cost:
        meal.normal_cost = round(meal.normal_cost * new / old, 2)
    build_shopping_list(db, meal.plan, user)
    db.commit()
    return _plan_payload(db, meal.plan, user)


@router.get("/meals/{meal_id}/options")
def meal_options(meal_id: int, user: CurrentUser, db: Db) -> list[dict]:
    meal = _get_meal(db, meal_id, user)
    _require_member(meal.plan, user)
    links = household_links(db, meal.plan.household_id)
    return [
        {
            "recipe_id": o.recipe.id,
            "title": o.recipe.title,
            "title_en": o.recipe.title_en,
            "cost": o.cost,
            "normal_cost": o.normal_cost,
            "reasons": o.reasons,
            "link": recipe_link(o.recipe, *links),
        }
        for o in replacement_options(db, meal, user, limit=5)
    ]


class AlternativeIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    recipe_id: int | None = None


@router.post("/meals/{meal_id}/alternatives", status_code=201)
def add_alternative(meal_id: int, payload: AlternativeIn, user: CurrentUser, db: Db) -> dict:
    """Someone in the family proposes something else, as free text or a known recipe."""
    meal = _get_meal(db, meal_id, user)
    if not can_vote(db, meal.plan, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cannot_vote")
    recipe = None
    if payload.recipe_id is not None:
        recipe = _visible_recipe(db, payload.recipe_id, meal.plan.household_id)
    title = (recipe.title if recipe else (payload.title or "").strip())
    if not title:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "title_required")
    db.add(MealAlternative(meal_id=meal.id, proposed_by=user.id, source="user", title=title, recipe_id=recipe.id if recipe else None))
    db.commit()
    return _plan_payload(db, meal.plan, user)


@router.post("/meals/{meal_id}/ai-alternative", status_code=201)
def ai_alternative(meal_id: int, user: CurrentUser, db: Db) -> dict:
    """Looks for a real recipe for this slot, falling back to the next best known one."""
    meal = _get_meal(db, meal_id, user)
    _require_member(meal.plan, user)

    household = household_of(db, user)
    source = "web"
    try:
        created = find_new_recipes(db, household, user, [meal.meal_type], 1, max(meal.date, local_today()), user.locale).saved
    except AiUnavailable:
        created = []
    if created:
        recipe = created[0]
    else:
        source = "system"
        options = replacement_options(db, meal, user, limit=1)
        if not options:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no_alternatives")
        recipe = options[0].recipe
    db.add(MealAlternative(meal_id=meal.id, proposed_by=user.id, source=source, title=recipe.title, recipe_id=recipe.id))
    db.commit()
    return _plan_payload(db, meal.plan, user)


class ReplaceIn(BaseModel):
    recipe_id: int


def _visible_recipe(db: Session, recipe_id: int, household_id: int) -> Recipe:
    recipe = db.get(Recipe, recipe_id)
    if recipe is None or recipe.household_id not in (None, household_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    return recipe


@router.post("/meals/{meal_id}/replace")
def replace(meal_id: int, payload: ReplaceIn, user: CurrentUser, db: Db) -> dict:
    meal = _get_meal(db, meal_id, user)
    _require_member(meal.plan, user)
    recipe = _visible_recipe(db, payload.recipe_id, meal.plan.household_id)
    try:
        replace_meal(db, meal, recipe, user)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except MealBought as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "meal_bought") from exc
    build_shopping_list(db, meal.plan, user)
    db.commit()
    return _plan_payload(db, meal.plan, user)


@router.post("/meals/{meal_id}/retry-sync")
def retry_sync(meal_id: int, user: CurrentUser, db: Db) -> dict:
    meal = _get_meal(db, meal_id, user)
    _require_member(meal.plan, user)
    # Only what has been sent once and failed. The first time is for the send button.
    if meal.status == "accepted" and meal.sync_status == "failed":
        meal.sync_status, meal.sync_attempts, meal.sync_error = "pending", 0, None
    db.commit()
    return {"sync_status": meal.sync_status}


@router.post("/meals/{meal_id}/decide")
def decide_directly(meal_id: int, payload: VoteIn, user: CurrentUser, db: Db) -> dict:
    """The household's admin or manager settles a meal without waiting for the vote."""
    meal = _get_meal(db, meal_id, user)
    _require_member(meal.plan, user)
    if not user.manages_household:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "manager_required")
    if meal.leftover_of is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "leftovers_follow")
    if payload.vote == "no":
        try:
            ensure_can_drop(meal)
        except MealBought as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "meal_bought") from exc
    change_status(db, meal, "accepted" if payload.vote == "yes" else "rejected", user)
    db.commit()
    return _plan_payload(db, meal.plan, user)


# Dishes chosen by hand


class AddMealIn(BaseModel):
    recipe_id: int
    date: date
    meal_type: MealType = "dinner"
    replace: bool = False


@router.post("/plans/add-meal")
def add_meal_to_day(payload: AddMealIn, user: CurrentUser, db: Db) -> dict:
    """Puts a dish on a day: in the plan that has that day, or a new one for its week."""
    household = household_of(db, user)
    recipe = _visible_recipe(db, payload.recipe_id, household.id)
    try:
        meal = add_meal(db, household, user, recipe, payload.date, payload.meal_type, payload.replace)
    except NotPlannable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    except SlotTaken as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "slot_taken") from exc
    except MealBought as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "meal_bought") from exc
    db.commit()
    return {"plan_id": meal.plan_id, "meal_id": meal.id, "plan": _plan_payload(db, meal.plan, user)}


@router.get("/recipes/{recipe_id}/planning")
def recipe_planning(recipe_id: int, user: CurrentUser, db: Db, day: date | None = None, meal_type: MealType | None = None) -> dict:
    """Where this dish is planned already, and what is planned on the day being looked at."""
    household = household_of(db, user)
    recipe = _visible_recipe(db, recipe_id, household.id)
    return planning_view(db, household, recipe, day, meal_type)


class BoughtIn(BaseModel):
    bought: Literal["none", "partial", "full"]


@router.post("/meals/{meal_id}/bought")
def meal_bought(meal_id: int, payload: BoughtIn, user: CurrentUser, db: Db) -> dict:
    meal = _get_meal(db, meal_id, user)
    _require_member(meal.plan, user)
    try:
        set_bought(db, meal, payload.bought)
    except NotPlannable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    db.commit()
    return _plan_payload(db, meal.plan, user)


class MoveIn(BaseModel):
    date: date
    # Trade places with a dish of the same meal already on that day.
    swap: bool = False


@router.post("/meals/{meal_id}/move")
def move(meal_id: int, payload: MoveIn, user: CurrentUser, db: Db) -> dict:
    """To another day in the same plan, or trading places with the dish there. Mealie follows the
    next time someone sends the plan."""
    meal = _get_meal(db, meal_id, user)
    _require_member(meal.plan, user)
    try:
        move_meal(db, meal, payload.date, user, payload.swap)
    except NotPlannable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    except SlotTaken as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "slot_taken") from exc
    db.commit()
    return _plan_payload(db, meal.plan, user)


class LeftoversIn(BaseModel):
    day: date | None = Field(default=None, alias="date")
    meal_type: MealType = "lunch"
    # Adult portions to cook extra. By default enough for the household's usual number.
    portions: float | None = Field(default=None, gt=0, le=50)


@router.post("/meals/{meal_id}/leftovers")
def leftovers(meal_id: int, payload: LeftoversIn, user: CurrentUser, db: Db) -> dict:
    """Cook more of this meal, and eat the rest at a later meal, by default lunch the next day."""
    meal = _get_meal(db, meal_id, user)
    _require_member(meal.plan, user)
    try:
        plan_leftovers(db, meal, user, payload.day, payload.meal_type, payload.portions)
    except NotPlannable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    except SlotTaken as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "slot_taken") from exc
    db.commit()
    return _plan_payload(db, meal.plan, user)


@router.delete("/meals/{meal_id}/leftovers")
def no_leftovers(meal_id: int, user: CurrentUser, db: Db) -> dict:
    meal = _get_meal(db, meal_id, user)
    _require_member(meal.plan, user)
    plan = meal.plan
    stop_leftovers(db, meal, user)
    db.commit()
    return _plan_payload(db, plan, user)


@router.post("/plans/{plan_id}/fill")
def fill(plan_id: int, user: CurrentUser, db: Db) -> dict:
    """Suggestions for the days still empty, to be voted on like any other."""
    plan = _get_plan(db, plan_id, user)
    _require_member(plan, user)
    added = fill_empty_slots(db, plan, user)
    db.commit()
    return {"added": added, "plan": _plan_payload(db, plan, user)}


@router.get("/offers/{offer_id}/dishes")
def offer_dishes(offer_id: int, user: CurrentUser, db: Db) -> list[dict]:
    """The household's dishes that use this offer."""
    offer = db.get(Offer, offer_id)
    if offer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    household = household_of(db, user)
    links = household_links(db, household.id)
    dishes = dishes_with_offer(db, household, user, offer)
    for dish in dishes:
        dish["link"] = recipe_link(db.get(Recipe, dish["id"]), *links)
    return dishes


# Recipes


def _recipe_payload(recipe: Recipe, links: tuple[str | None, str | None] = (None, None)) -> dict:
    return {
        "id": recipe.id,
        "title": recipe.title,
        "title_en": recipe.title_en,
        "description": recipe.description,
        "meal_types": recipe.meal_types,
        "servings": recipe.servings,
        "ingredients": recipe.ingredients,
        "steps": recipe.steps,
        "steps_en": recipe.steps_en,
        "tags": recipe.tags,
        "source": recipe.source,
        "source_url": recipe.source_url,
        "image_url": recipe.image_url,
        "link": recipe_link(recipe, *links),
        "mealie_slug": recipe.mealie_slug,
    }


@router.get("/recipes")
def list_recipes(user: CurrentUser, db: Db, q: str | None = None, meal_type: str | None = None) -> list[dict]:
    needle = normalize(q)
    profile = household_profile(db, household_of(db, user))
    links = household_links(db, user.household_id)
    results = []
    for recipe in visible_recipes(db, user.household_id):
        if meal_type and meal_type not in (recipe.meal_types or []):
            continue
        if needle and needle not in normalize(recipe.title):
            continue
        payload = _recipe_payload(recipe, links)
        payload["allergy_conflict"] = allergy_conflict(recipe, profile.allergies)
        results.append(payload)
    return sorted(results, key=lambda r: r["title"])


@router.get("/recipes/{recipe_id}")
def get_recipe(recipe_id: int, user: CurrentUser, db: Db) -> dict:
    return _recipe_payload(_visible_recipe(db, recipe_id, user.household_id), household_links(db, user.household_id))


# Calendar


@router.get("/calendar")
def calendar(user: CurrentUser, db: Db, start: date | None = None, end: date | None = None) -> dict:
    """Planned meals together with anything added directly in Mealie's calendar."""
    first = start or local_today()
    last = end or first + timedelta(days=13)
    if (last - first).days > 92:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "range_too_long")
    plans = list(
        db.scalars(
            select(MealPlan).where(
                MealPlan.household_id == user.household_id, MealPlan.start_date <= last, MealPlan.end_date >= first
            )
        )
    )
    meals = [
        {"id": m.id, "date": m.date, "meal_type": m.meal_type, "title": m.title, "status": m.status, "plan_id": p.id, "sync_status": m.sync_status}
        for p in plans
        for m in p.meals
        if first <= m.date <= last and m.status != "rejected"
    ]
    return {
        "start": first,
        "end": last,
        "meals": sorted(meals, key=lambda m: m["date"]),
        "mealie": calendar_entries(db, plans, user.household_id, first, last),
    }


# Shopping lists


def _get_list(db: Session, list_id: int, user: User) -> ShoppingList:
    shopping = db.get(ShoppingList, list_id)
    if shopping is None or shopping.household_id != user.household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    return shopping


@router.get("/shopping-lists")
def list_shopping_lists(user: CurrentUser, db: Db) -> list[dict]:
    rows = db.scalars(
        select(ShoppingList).where(ShoppingList.household_id == user.household_id).order_by(ShoppingList.id.desc()).limit(20)
    )
    return [
        {"id": s.id, "name": s.name, "plan_id": s.plan_id, "completed_at": s.completed_at, "total": (s.summary or {}).get("total"), "savings": (s.summary or {}).get("savings")}
        for s in rows
    ]


@router.get("/shopping-lists/{list_id}")
def get_shopping_list(list_id: int, user: CurrentUser, db: Db) -> dict:
    return _list_payload(_get_list(db, list_id, user))


class ManualItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    quantity: float | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=12)


@router.post("/shopping-lists/{list_id}/items", status_code=201)
def add_item(list_id: int, payload: ManualItemIn, user: CurrentUser, db: Db) -> dict:
    shopping = _get_list(db, list_id, user)
    from app.services.allergens import conflicting_allergy

    profile = household_profile(db, household_of(db, user))
    shopping.items.append(ShoppingItem(name=payload.name, quantity=payload.quantity, unit=payload.unit, manual=True))
    db.commit()
    result = _list_payload(shopping)
    # People may buy what they want. They should still hear about it if it is on the allergy list.
    result["allergy_warning"] = conflicting_allergy(payload.name, profile.allergies)
    return result


class ItemIn(BaseModel):
    checked: bool


@router.patch("/shopping-items/{item_id}")
def update_item(item_id: int, payload: ItemIn, user: CurrentUser, db: Db) -> dict:
    item = db.get(ShoppingItem, item_id)
    if item is None or item.shopping_list.household_id != user.household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    item.checked = payload.checked
    db.commit()
    return {"id": item.id, "checked": item.checked}


@router.post("/shopping-items/{item_id}/wrong-match")
def wrong_match(item_id: int, user: CurrentUser, db: Db) -> dict:
    """"That offer is not this ingredient." Remembered for the household, and the list is redone."""
    item = db.get(ShoppingItem, item_id)
    if item is None or item.shopping_list.household_id != user.household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    shopping = item.shopping_list
    if item.offer_title:
        household = household_of(db, user)
        hs = household_settings(household.settings)
        key = block_key(item.name, item.offer_title)
        if key not in hs.blocked_matches:
            hs.blocked_matches.append(key)
            household.settings = hs.model_dump(mode="json")
            db.flush()
    if shopping.plan_id:
        build_shopping_list(db, db.get(MealPlan, shopping.plan_id), user)
    db.commit()
    return _list_payload(shopping)


@router.delete("/shopping-items/{item_id}")
def delete_item(item_id: int, user: CurrentUser, db: Db) -> dict:
    item = db.get(ShoppingItem, item_id)
    if item is None or item.shopping_list.household_id != user.household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    db.delete(item)
    db.commit()
    return {"ok": True}


class ReceiptLineIn(BaseModel):
    item_id: int | None = None
    name: str = Field(min_length=1, max_length=200)
    quantity: float | None = Field(default=None, gt=0, lt=100000)
    unit: Literal["g", "kg", "ml", "l", "stk"] | None = None
    price: float = Field(gt=0, le=5000)


class CompleteIn(BaseModel):
    amount: float | None = Field(default=None, ge=0)
    day: date | None = None
    # The lines a person confirmed from a photo of the receipt.
    receipt: list[ReceiptLineIn] | None = Field(default=None, max_length=150)


@router.post("/shopping-lists/{list_id}/complete")
def complete(list_id: int, payload: CompleteIn, user: CurrentUser, db: Db) -> dict:
    shopping = _get_list(db, list_id, user)
    if shopping.completed_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "already_completed")
    receipt = [line.model_dump() for line in payload.receipt] if payload.receipt else None
    if receipt:
        own = {i.id for i in shopping.items}
        if any(line["item_id"] is not None and line["item_id"] not in own for line in receipt):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    expense = complete_shopping_list(db, shopping, user, payload.amount, payload.day, receipt)
    db.commit()
    return {"expense_id": expense.id, "amount": expense.amount, "savings": expense.savings, "basis": expense.note}


@router.post("/shopping-lists/{list_id}/receipt")
async def read_receipt_photo(list_id: int, user: CurrentUser, db: Db, photos: list[UploadFile] = File(...)) -> dict:
    """Reads a photo of the receipt and matches it to the list. Nothing is saved: the person checks
    the reading and confirms it when finishing the trip."""
    shopping = _get_list(db, list_id, user)
    if shopping.completed_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "already_completed")
    if len(photos) > MAX_RECEIPT_PHOTOS:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "too_many_photos")
    images: list[tuple[bytes, str]] = []
    for photo in photos:
        if photo.content_type not in RECEIPT_TYPES:
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "unsupported_image_type")
        data = await photo.read(MAX_RECEIPT_BYTES + 1)
        if len(data) > MAX_RECEIPT_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "photo_too_large")
        images.append((data, photo.content_type))

    from app.ai.features import read_receipt
    from app.services.app_settings import get_ai_settings

    ai = get_ai_settings(db)
    try:
        # A worker thread, as the AI engine runs its own event loop.
        receipt = await run_in_threadpool(read_receipt, images, user.household_id, user.id, ai.vision_model, user.locale)
    except AiUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"ai_unavailable:{exc.reason}") from exc
    return receipt_proposal(shopping, receipt)


@router.post("/plans/{plan_id}/refresh-list")
def refresh_list(plan_id: int, user: CurrentUser, db: Db) -> dict:
    plan = _get_plan(db, plan_id, user)
    _require_member(plan, user)
    shopping = build_shopping_list(db, plan, user)
    db.commit()
    return _list_payload(shopping)
