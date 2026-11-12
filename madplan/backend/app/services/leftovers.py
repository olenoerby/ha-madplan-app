"""Leftovers planned on purpose: a dinner cooked for more, and the lunch that eats the rest.

The leftovers meal has no recipe of its own. Its food is bought and taken from the pantry with the
meal it was cooked with, whose portions include it, and it follows that meal's decision.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Household, PlannedMeal
from app.services.recipes import portions
from app.settings_schema import household_settings


class FollowsAnotherMeal(Exception):
    """Leftovers are decided with the meal they were cooked with, not voted on by themselves."""


def leftovers_of(db: Session, meal: PlannedMeal) -> list[PlannedMeal]:
    if meal.id is None:
        return []
    return list(db.scalars(select(PlannedMeal).where(PlannedMeal.leftover_of == meal.id)))


def follow(db: Session, meal: PlannedMeal) -> None:
    """Leftovers take the decision of the meal they come from."""
    for left in leftovers_of(db, meal):
        left.status = meal.status
        if left.sync_status == "failed":
            left.sync_status, left.sync_error, left.sync_attempts = "none", None, 0


def drop_leftovers(db: Session, meal: PlannedMeal) -> int:
    """Stops cooking extra for later. What was sent to Mealie goes on the next send."""
    dropped = 0
    for left in leftovers_of(db, meal):
        if left.mealie_entry_id or left.mealie_note_id:
            left.status = "rejected"
            left.leftover_of = None
        else:
            if left in meal.plan.meals:
                meal.plan.meals.remove(left)
            db.delete(left)
        dropped += 1
    if meal.leftover_portions:
        set_leftover_portions(db, meal, 0)
    db.flush()
    return dropped


def set_leftover_portions(db: Session, meal: PlannedMeal, extra: float) -> None:
    """Cooks this much more, and the estimate grows or shrinks with it."""
    child_portion = household_settings(db.get(Household, meal.plan.household_id).settings).child_portion
    base = portions(meal.adults, meal.children, child_portion)
    factor = (base + extra) / (base + (meal.leftover_portions or 0))
    meal.leftover_portions = extra
    meal.estimated_cost = round(meal.estimated_cost * factor, 2)
    if meal.normal_cost:
        meal.normal_cost = round(meal.normal_cost * factor, 2)


def retitle(db: Session, meal: PlannedMeal) -> None:
    """The dish changed, so its leftovers have a new name."""
    for left in leftovers_of(db, meal):
        prefix = left.title.split(": ", 1)[0] if ": " in left.title else "Rester"
        left.title = f"{prefix}: {meal.title}"[:200]


def forget_source(db: Session, left: PlannedMeal) -> None:
    """A leftovers meal is gone, so the meal it came from no longer needs to make extra."""
    if left.leftover_of is None:
        return
    source = db.get(PlannedMeal, left.leftover_of)
    if source is not None and source.leftover_portions:
        set_leftover_portions(db, source, 0)
