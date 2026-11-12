"""Mealie has the last word.

When Mealie is set up, what people change there wins over what MadPlan has:

- A dish MadPlan sent that is moved, given another meal or another recipe in Mealie, is changed
  the same way here. One removed in Mealie is turned down here.
- A dish put on Mealie's meal planner directly comes onto the plan that covers its day, as
  accepted. MadPlan keeps track of it from then on, so it is not sent back.
- What is ticked off on Mealie's shopping list is ticked off on MadPlan's.

A change made here and not yet sent gives way to a change made in Mealie meanwhile.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import ha
from app.core.db import local_today
from app.integrations.mealie import MealieClient, MealieError
from app.models import MealPlan, PlannedMeal, Recipe, ShoppingList
from app.services.mealie_sync import is_marked
from app.services.shopping import build_shopping_list
from app.services.text import normalize, text_matches
from app.services.voting import change_status

log = logging.getLogger(__name__)

# How far ahead Mealie's planner is read. Plans do not reach further than this.
WINDOW_DAYS = 45
_MEAL_TYPES = {"breakfast", "lunch", "dinner", "snack", "drink"}


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _meal_type(entry: dict) -> str:
    kind = entry.get("entryType") or "dinner"
    return kind if kind in _MEAL_TYPES else "dinner"


def _plan_for(plans: list[MealPlan], day: date) -> MealPlan | None:
    covering = [p for p in plans if p.start_date <= day <= p.end_date]
    return max(covering, key=lambda p: p.id) if covering else None


def _record(meal: PlannedMeal, **changes) -> None:
    meal.mealie_sent = {**(meal.mealie_sent or {}), **changes}


def pull_plan(db: Session, household_id: int, client: MealieClient, today: date | None = None) -> dict:
    """Brings Mealie's meal planner into MadPlan's plans. Returns what changed, by kind."""
    today = today or local_today()
    last = today + timedelta(days=WINDOW_DAYS)
    entries = client.list_plan_entries(today, last)
    by_id = {str(e.get("id")): e for e in entries}
    plans = list(db.scalars(select(MealPlan).where(MealPlan.household_id == household_id, MealPlan.end_date >= today)))
    meals = [m for plan in plans for m in plan.meals]
    notes = {str(m.mealie_note_id) for m in meals if m.mealie_note_id}
    known = {str(m.mealie_entry_id) for m in meals if m.mealie_entry_id} | notes
    recipes_by_mealie_id = {
        r.mealie_id: r for r in db.scalars(select(Recipe).where(Recipe.household_id == household_id, Recipe.mealie_id.is_not(None)))
    }
    changed = {"moved": 0, "changed": 0, "removed": 0, "added": 0}
    touched: set[int] = set()

    for meal in meals:
        if not meal.mealie_entry_id or meal.date < today or meal.status == "rejected":
            continue
        entry = by_id.get(str(meal.mealie_entry_id))
        if entry is None:
            # Removed in Mealie. Turned down here too, and nothing of it is sent back.
            change_status(db, meal, "rejected", None)
            meal.mealie_entry_id = None
            meal.mealie_sent = None
            meal.sync_status, meal.sync_error = "none", None
            changed["removed"] += 1
            touched.add(meal.plan_id)
            continue
        day = _day(entry.get("date"))
        if day and day != meal.date:
            target = _plan_for(plans, day)
            if target is None:
                # Moved outside every plan: it is Mealie's alone now, and shown as such.
                change_status(db, meal, "rejected", None)
                meal.mealie_entry_id, meal.mealie_sent = None, None
                changed["removed"] += 1
            else:
                meal.plan_id = target.id
                meal.date = day
                _record(meal, date=day.isoformat())
                changed["moved"] += 1
                touched.add(target.id)
            touched.add(meal.plan_id)
        kind = _meal_type(entry)
        if kind != meal.meal_type:
            meal.meal_type = kind
            changed["changed"] += 1
            touched.add(meal.plan_id)
        recipe_id = entry.get("recipeId")
        if recipe_id and recipe_id != (meal.mealie_sent or {}).get("mealie_recipe_id"):
            recipe = recipes_by_mealie_id.get(recipe_id)
            meal.recipe_id, meal.recipe = (recipe.id, recipe) if recipe else (None, None)
            meal.title = (recipe.title if recipe else (entry.get("recipe") or {}).get("name") or entry.get("title") or meal.title)[:200]
            _record(meal, recipe_id=meal.recipe_id, mealie_recipe_id=recipe_id)
            changed["changed"] += 1
            touched.add(meal.plan_id)
        # Whatever was waiting to be sent from here gives way.
        if meal.sync_status == "pending":
            meal.sync_status = "none"

    for entry_id, entry in by_id.items():
        if entry_id in known or is_marked(entry):
            continue
        day = _day(entry.get("date"))
        plan = _plan_for(plans, day) if day else None
        if plan is None or day < today:
            continue
        recipe = recipes_by_mealie_id.get(entry.get("recipeId") or "")
        title = (recipe.title if recipe else (entry.get("recipe") or {}).get("name") or entry.get("title") or "").strip()
        if not title:
            continue
        meal = PlannedMeal(
            plan_id=plan.id,
            date=day,
            meal_type=_meal_type(entry),
            recipe_id=recipe.id if recipe else None,
            title=title[:200],
            status="accepted",
            reasons=[{"code": "from_mealie"}],
            mealie_entry_id=entry_id,
            sync_status="synced",
        )
        # So that it is never sent back to Mealie as something new.
        meal.mealie_sent = {
            "recipe_id": meal.recipe_id,
            "mealie_recipe_id": entry.get("recipeId"),
            "scale": entry.get("recipeScale"),
            "date": day.isoformat(),
            "listed": True,
        }
        plan.meals.append(meal)
        changed["added"] += 1
        touched.add(plan.id)

    db.flush()
    for plan in plans:
        if plan.id in touched:
            build_shopping_list(db, plan, None)
    return changed


def _item_name(item: dict) -> str:
    food = item.get("food") or {}
    return str(food.get("name") or item.get("note") or item.get("display") or "")


def pull_ticks(db: Session, household_id: int, client: MealieClient, list_id: str) -> int:
    """What is ticked off on Mealie's list is ticked off here, and the other way round."""
    remote = client.get_shopping_list(list_id).get("listItems") or []
    status = {}
    for item in remote:
        name = normalize(_item_name(item))
        if name:
            # Ticked anywhere wins, as two lines can be the same thing.
            status[name] = status.get(name, False) or bool(item.get("checked"))
    lists = db.scalars(
        select(ShoppingList).where(ShoppingList.household_id == household_id, ShoppingList.completed_at.is_(None))
    )
    changed = 0
    for shopping in lists:
        for item in shopping.items:
            if item.covered_by_pantry:
                continue
            mine = normalize(item.name)
            remote_state = status.get(mine)
            if remote_state is None:
                remote_state = next((state for name, state in status.items() if text_matches(mine, name)), None)
            if remote_state is not None and remote_state != item.checked:
                item.checked = remote_state
                changed += 1
    db.flush()
    return changed


def pull_from_mealie(db: Session, household_id: int, client: MealieClient, list_id: str | None = None) -> dict:
    result = pull_plan(db, household_id, client)
    try:
        target = list_id or next((s["id"] for s in client.shopping_lists() if s.get("name") == ha.options().mealie_shopping_list), None)
        result["ticked"] = pull_ticks(db, household_id, client, target) if target else 0
    except MealieError as exc:
        log.info("could not read Mealie's shopping list: %s", exc)
        result["ticked"] = 0
    return result
