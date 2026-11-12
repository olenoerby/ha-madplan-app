"""Dishes people put on a day themselves, from an offer or a suggestion.

Choosing a dish counts as the chooser's own yes, and the rest of the household votes as usual.
Everyone is told when the same dish is already planned nearby, using the household's repeat
window. Once the food for a meal is bought, it can be moved but not voted off.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.db import local_today
from app.models import Household, MealPlan, Offer, PlanDay, PlannedMeal, Recipe, User
from app.services.catalog import find_ingredient
from app.services.leftovers import drop_leftovers, leftovers_of, set_leftover_portions
from app.services.notifications import notify
from app.services.planner import build_context, score_recipe
from app.services.pricing import PriceBook, is_staple, load_offer_views
from app.services.recipes import allergy_conflict, eligible_recipes, household_profile, portions, visible_recipes
from app.services.shopping import build_shopping_list
from app.services.text import normalize
from app.services.voting import MealBought, can_vote, cast_vote
from app.settings_schema import household_settings, user_settings

BOUGHT_STATES = ("none", "partial", "full")
# How far ahead a dish can be put on a day. Further than this is not a plan, but a wish.
MAX_DAYS_AHEAD = 62
_MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack", "drink")


class SlotTaken(Exception):
    """Another dish is already planned for that meal on that day."""


class NotPlannable(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _is_live(meal: PlannedMeal) -> bool:
    return meal.status != "rejected"


def occupant(plan: MealPlan, day: date, meal_type: str, leave_out: int | None = None) -> PlannedMeal | None:
    """The dish already planned for this meal on this day, if any."""
    return next(
        (m for m in plan.meals if m.date == day and m.meal_type == meal_type and _is_live(m) and m.id != leave_out),
        None,
    )


def plan_covering(db: Session, household_id: int, day: date) -> MealPlan | None:
    """The newest plan that has this day, as plans for the same days can overlap."""
    return db.scalar(
        select(MealPlan)
        .where(MealPlan.household_id == household_id, MealPlan.start_date <= day, MealPlan.end_date >= day)
        .order_by(MealPlan.id.desc())
        .limit(1)
    )


def _new_week_plan(db: Session, household: Household, day: date, user: User) -> MealPlan:
    """An empty Monday-to-Sunday plan, for the rest of the week to be filled in later."""
    hs = household_settings(household.settings)
    monday = day - timedelta(days=day.weekday())
    plan = MealPlan(
        household_id=household.id,
        created_by=user.id,
        start_date=monday,
        end_date=monday + timedelta(days=6),
        meal_types=list(hs.default_meal_types),
        budget=hs.budget_weekly,
    )
    db.add(plan)
    db.flush()
    for offset in range(7):
        current = monday + timedelta(days=offset)
        db.add(PlanDay(plan_id=plan.id, date=current, skipped=current != day and current.weekday() in hs.skip_weekdays))
    db.flush()
    db.refresh(plan)
    return plan


def same_dish(meal: PlannedMeal, recipe_id: int | None, title: str) -> bool:
    if recipe_id is not None and meal.recipe_id == recipe_id:
        return True
    return normalize(meal.title) == normalize(title)


def planned_nearby(
    db: Session,
    household_id: int,
    recipe_id: int | None,
    title: str,
    first: date,
    last: date,
    leave_out: int | None = None,
) -> list[PlannedMeal]:
    """The same dish planned between two dates, and not refused or left unmade."""
    rows = db.scalars(
        select(PlannedMeal)
        .join(MealPlan, MealPlan.id == PlannedMeal.plan_id)
        .where(
            MealPlan.household_id == household_id,
            PlannedMeal.date >= first,
            PlannedMeal.date <= last,
            PlannedMeal.status != "rejected",
            or_(PlannedMeal.cooked.is_(None), PlannedMeal.cooked.is_(True)),
        )
        .order_by(PlannedMeal.date)
    )
    return [m for m in rows if m.id != leave_out and same_dish(m, recipe_id, title)]


def nearby_payload(meal: PlannedMeal) -> dict:
    return {"meal_id": meal.id, "plan_id": meal.plan_id, "date": meal.date, "meal_type": meal.meal_type, "status": meal.status, "title": meal.title}


def also_planned(db: Session, plan: MealPlan) -> dict[int, list[dict]]:
    """For each meal in a plan, the same dish planned elsewhere within the household's repeat window."""
    household = db.get(Household, plan.household_id)
    window = household_settings(household.settings).repeat_avoid_days
    if window <= 0 or not plan.meals:
        return {}
    rows = list(
        db.scalars(
            select(PlannedMeal)
            .join(MealPlan, MealPlan.id == PlannedMeal.plan_id)
            .where(
                MealPlan.household_id == plan.household_id,
                PlannedMeal.date >= plan.start_date - timedelta(days=window),
                PlannedMeal.date <= plan.end_date + timedelta(days=window),
                PlannedMeal.status != "rejected",
                or_(PlannedMeal.cooked.is_(None), PlannedMeal.cooked.is_(True)),
            )
            .order_by(PlannedMeal.date)
        )
    )
    result: dict[int, list[dict]] = {}
    for meal in plan.meals:
        if not _is_live(meal):
            continue
        others = [
            nearby_payload(m)
            for m in rows
            if m.id != meal.id and abs((m.date - meal.date).days) <= window and same_dish(m, meal.recipe_id, meal.title)
        ]
        if others:
            result[meal.id] = others
    return result


def planning_view(db: Session, household: Household, recipe: Recipe, day: date | None, meal_type: str | None) -> dict:
    """What someone choosing a day for a dish should know: where it is planned already, and what is
    on the day they are looking at."""
    window = household_settings(household.settings).repeat_avoid_days
    today = local_today()
    first = min(today, day or today) - timedelta(days=window)
    last = max(today, day or today) + timedelta(days=max(window, 14))
    planned = planned_nearby(db, household.id, recipe.id, recipe.title, first, last)
    slot = None
    if day is not None and meal_type:
        plan = plan_covering(db, household.id, day)
        taken = occupant(plan, day, meal_type) if plan else None
        if taken is not None:
            slot = {"meal_id": taken.id, "title": taken.title, "status": taken.status, "bought": taken.bought}
    return {
        "window_days": window,
        "planned": [nearby_payload(m) for m in planned],
        "too_close": [nearby_payload(m) for m in planned if day is not None and abs((m.date - day).days) <= window],
        "slot": slot,
        "allergy": allergy_conflict(recipe, household_profile(db, household).allergies),
    }


def add_meal(
    db: Session,
    household: Household,
    user: User,
    recipe: Recipe,
    day: date,
    meal_type: str,
    replace: bool = False,
) -> PlannedMeal:
    """Puts a dish on a day. Raises NotPlannable, SlotTaken or MealBought."""
    today = local_today()
    if day < today or day > today + timedelta(days=MAX_DAYS_AHEAD):
        raise NotPlannable("date_out_of_range")
    if meal_type not in _MEAL_TYPES:
        raise NotPlannable("meal_type")
    conflict = allergy_conflict(recipe, household_profile(db, household).allergies)
    if conflict:
        raise NotPlannable(f"allergy:{conflict}")

    plan = plan_covering(db, household.id, day)
    created = plan is None
    if plan is None:
        plan = _new_week_plan(db, household, day, user)
    day_row = next((d for d in plan.days if d.date == day), None)
    if day_row is None:
        day_row = PlanDay(plan_id=plan.id, date=day)
        db.add(day_row)
    # Choosing a dish for a day someone had skipped means they will be eating after all.
    day_row.skipped = False

    taken = occupant(plan, day, meal_type)
    if taken is not None:
        if not replace:
            raise SlotTaken(taken.title)
        if taken.bought == "full":
            raise MealBought("meal_bought")

    ctx = build_context(db, household, max(plan.start_date, today), user)
    adults = ctx.settings.default_adults + day_row.guest_adults
    children = ctx.settings.default_children + day_row.guest_children
    item = score_recipe(recipe, portions(adults, children, ctx.settings.child_portion), day, ctx, Counter(), frozenset())
    if taken is not None:
        meal = taken
        meal.votes.clear()
        meal.status = "pending"
        meal.sync_status, meal.sync_error, meal.sync_attempts = "none", None, 0
        meal.bought = "none"
    else:
        meal = PlannedMeal(plan_id=plan.id, date=day, meal_type=meal_type, adults=adults, children=children)
        plan.meals.append(meal)
    meal.recipe_id, meal.recipe, meal.title = recipe.id, recipe, recipe.title
    meal.estimated_cost, meal.normal_cost = item.cost, item.normal_cost
    meal.reasons = [{"code": "chosen_by_hand"}, *item.reasons]
    meal.added_by = user.id
    meal.cooked = None
    db.flush()

    # The one who chose it has said yes, and the rest of the household votes as usual.
    if can_vote(db, plan, user):
        cast_vote(db, meal, user, "yes")
    build_shopping_list(db, plan, user)
    _tell_the_others(db, meal, user, created)
    return meal


def _tell_the_others(db: Session, meal: PlannedMeal, made_by: User, new_plan: bool) -> None:
    members = db.scalars(select(User).where(User.household_id == meal.plan.household_id, User.is_active.is_(True), User.id != made_by.id))
    data = {"plan_id": meal.plan_id, "meal_id": meal.id, "title": meal.title, "date": meal.date.isoformat(), "meal_type": meal.meal_type, "by": made_by.display_name, "new_plan": new_plan}
    for member in members:
        if user_settings(member.settings).features.plan_updates:
            notify(db, member, "meal_added", data, dedupe_key=f"meal_added:{meal.id}:{meal.recipe_id}")


def set_bought(db: Session, meal: PlannedMeal, state: str) -> None:
    if state not in BOUGHT_STATES:
        raise NotPlannable("bought_state")
    if meal.status == "rejected" and state != "none":
        raise NotPlannable("meal_rejected")
    meal.bought = state
    db.flush()


def _record_sent_day(meal: PlannedMeal) -> None:
    if meal.mealie_entry_id and meal.mealie_sent is not None and "date" not in meal.mealie_sent:
        # Sent before dates were recorded. Mealie has it on the day it had until now.
        meal.mealie_sent = {**meal.mealie_sent, "date": meal.date.isoformat()}


def _set_day(meal: PlannedMeal, day: date) -> None:
    _record_sent_day(meal)
    meal.date = day
    if meal.sync_status == "failed":
        meal.sync_status, meal.sync_error, meal.sync_attempts = "none", None, 0


def move_meal(db: Session, meal: PlannedMeal, day: date, user: User | None, swap: bool = False) -> PlannedMeal | None:
    """To another day of the same plan, as its food is on that plan's list.

    With ``swap``, a dish already on that day takes this one's place instead. Returns it."""
    plan = meal.plan
    today = local_today()
    if day < today or not (plan.start_date <= day <= plan.end_date):
        raise NotPlannable("date_not_in_plan")
    if meal.date < today:
        raise NotPlannable("meal_is_past")
    if day == meal.date:
        return None
    source = db.get(PlannedMeal, meal.leftover_of) if meal.leftover_of else None
    if source is not None and day < source.date:
        raise NotPlannable("leftovers_before_meal")
    taken = occupant(plan, day, meal.meal_type, leave_out=meal.id)
    if taken is not None and not swap:
        raise SlotTaken(taken.title)
    for current in (day, meal.date):
        day_row = next((d for d in plan.days if d.date == current), None)
        if day_row is None:
            db.add(PlanDay(plan_id=plan.id, date=current))
        else:
            day_row.skipped = False
    old_day = meal.date
    _set_day(meal, day)
    _carry_leftovers(db, meal, (day - old_day).days)
    if taken is not None:
        _set_day(taken, old_day)
        _carry_leftovers(db, taken, (old_day - day).days)
    db.flush()
    build_shopping_list(db, plan, user)
    return taken


def _carry_leftovers(db: Session, meal: PlannedMeal, shift: int) -> None:
    """Leftovers move with the meal they come from. Where they cannot, they are dropped, as eating
    them before the meal is cooked does not work."""
    plan = meal.plan
    for left in leftovers_of(db, meal):
        target = left.date + timedelta(days=shift)
        if target < local_today() or not (plan.start_date <= target <= plan.end_date) or occupant(plan, target, left.meal_type, leave_out=left.id):
            drop_leftovers(db, meal)
            return
        _set_day(left, target)


def plan_leftovers(db: Session, meal: PlannedMeal, user: User, day: date | None = None, meal_type: str = "lunch", extra: float | None = None) -> PlannedMeal:
    """Cooks more of this meal, and puts its leftovers on a later meal. Raises NotPlannable or SlotTaken."""
    if meal.leftover_of is not None:
        raise NotPlannable("leftovers_of_leftovers")
    if meal.recipe is None:
        raise NotPlannable("no_recipe")
    if meal.status == "rejected":
        raise NotPlannable("meal_rejected")
    if leftovers_of(db, meal):
        raise NotPlannable("leftovers_planned")
    if meal_type not in _MEAL_TYPES:
        raise NotPlannable("meal_type")
    plan = meal.plan
    day = day or meal.date + timedelta(days=1)
    if day < meal.date or (day == meal.date and _MEAL_TYPES.index(meal_type) <= _MEAL_TYPES.index(meal.meal_type)):
        raise NotPlannable("leftovers_before_meal")
    if day < local_today() or not (plan.start_date <= day <= plan.end_date):
        raise NotPlannable("date_not_in_plan")
    taken = occupant(plan, day, meal_type)
    if taken is not None:
        raise SlotTaken(taken.title)
    hs = household_settings(db.get(Household, plan.household_id).settings)
    if extra is None:
        extra = portions(hs.default_adults, hs.default_children, hs.child_portion)
    set_leftover_portions(db, meal, extra)
    day_row = next((d for d in plan.days if d.date == day), None)
    if day_row is None:
        day_row = PlanDay(plan_id=plan.id, date=day)
        db.add(day_row)
    day_row.skipped = False
    prefix = "Leftovers" if user.locale == "en" else "Rester"
    left = PlannedMeal(
        plan_id=plan.id,
        date=day,
        meal_type=meal_type,
        recipe_id=None,
        title=f"{prefix}: {meal.title}"[:200],
        adults=hs.default_adults,
        children=hs.default_children,
        status=meal.status,
        estimated_cost=0,
        reasons=[{"code": "leftovers", "term": meal.title}],
        leftover_of=meal.id,
        added_by=user.id,
        bought=meal.bought,
    )
    plan.meals.append(left)
    db.flush()
    build_shopping_list(db, plan, user)
    return left


def stop_leftovers(db: Session, meal: PlannedMeal, user: User | None) -> None:
    """From either end: the meal cooked for more, or the leftovers meal."""
    source = db.get(PlannedMeal, meal.leftover_of) if meal.leftover_of else meal
    if source is None:
        return
    drop_leftovers(db, source)
    build_shopping_list(db, source.plan, user)


def dishes_with_offer(db: Session, household: Household, user: User, offer: Offer, limit: int = 12) -> list[dict]:
    """The household's dishes that use this offer, best first, whether or not its store is chosen."""
    today = local_today()
    views = [v for v in load_offer_views(db, {offer.store_id}, today) if v.id == offer.id]
    if not views:
        return []
    profile = household_profile(db, household)
    hs = household_settings(household.settings)
    # A book with only this offer in it: anything that matches it uses this offer. It also
    # applies the allergy check on the offer's own text.
    single = PriceBook(views, profile.allergies, hs.blocked_matches)
    if not single.offers:
        return []
    recipes = visible_recipes(db, household.id)
    pool = {r.id: r for meal_type in _MEAL_TYPES for r in eligible_recipes(recipes, meal_type, profile)}
    matching = [r for r in pool.values() if _uses(r, single)]
    if not matching:
        return []
    ctx = build_context(db, household, today, user)
    people = portions(ctx.settings.default_adults, ctx.settings.default_children, ctx.settings.child_portion)
    scored = sorted((score_recipe(r, people, today, ctx, Counter(), frozenset()) for r in matching), key=lambda s: -s.score)
    return [
        {
            "id": s.recipe.id,
            "title": s.recipe.title,
            "title_en": s.recipe.title_en,
            "meal_types": s.recipe.meal_types,
            "cost": s.cost,
            "people": people,
            "image_url": s.recipe.image_url,
        }
        for s in scored[:limit]
    ]


def _uses(recipe: Recipe, book: PriceBook) -> bool:
    for line in recipe.ingredients or []:
        name = line.get("name", "")
        if not name or is_staple(name):
            continue
        if book.matches(name, find_ingredient(name)):
            return True
    return False
