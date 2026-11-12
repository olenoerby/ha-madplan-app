"""What is at home: what can be counted on, and how food comes in and goes out again.

Food comes in when a shopping trip is finished, as whole packs, since that is what comes home.
It goes out when a meal has been eaten. Without that second half, everything ever bought would
count as being at home for good, and each new plan would buy too little.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.core.db import local_today, utcnow
from app.models import Household, MealPlan, PantryItem, PlannedMeal, ShoppingItem, ShoppingList
from app.services.catalog import Ingredient, find_ingredient, keeps_days
from app.services.pricing import key_for
from app.services.recipes import meal_portions
from app.services.text import to_base_unit
from app.settings_schema import household_settings

# Meals older than this that nobody accepted are left alone: they were never part of anything.
_CONSUME_LOOKBACK_DAYS = 14
# An estimated date this far back means the food has been eaten or thrown out long ago.
_FORGET_ESTIMATED_AFTER_DAYS = 14
_EMPTY = 0.0005


def is_available(item: PantryItem, on_day: date) -> bool:
    """Whether a plan can count on this. Empty is empty, and food past its date is bought anew."""
    if item.quantity is not None and item.quantity <= _EMPTY:
        return False
    return item.expires_on is None or item.expires_on >= on_day


def amount_in(item: PantryItem, unit: str | None, ingredient: Ingredient | None) -> float | None:
    """The item's amount in the unit a recipe asks for, or None when the two cannot be compared."""
    quantity, base = to_base_unit(item.quantity, item.unit)
    if quantity is None:
        return None
    base = base or "stk"
    if base == unit or {base, unit} == {"g", "ml"}:
        return quantity
    if ingredient is not None and ingredient.piece_g:
        if unit == "stk" and base in ("g", "ml"):
            return quantity / ingredient.piece_g
        if base == "stk" and unit in ("g", "ml"):
            return quantity * ingredient.piece_g
    return None


@dataclass
class Stock:
    """What can be counted on, by ingredient."""

    items: dict[str, list[PantryItem]]

    def has(self, key: str) -> bool:
        return key in self.items

    def amount(self, key: str, unit: str | None) -> float | None:
        """How much there is, or None when the pantry says "yes" without saying how much."""
        ingredient = find_ingredient(key)
        total = 0.0
        for item in self.items.get(key, []):
            amount = amount_in(item, unit, ingredient)
            if amount is None:
                return None
            total += amount
        return total

    def names(self) -> list[str]:
        return [item.name for rows in self.items.values() for item in rows]


def load_stock(db: Session, household_id: int, on_day: date | None = None) -> Stock:
    day = on_day or local_today()
    items: dict[str, list[PantryItem]] = {}
    for item in db.scalars(select(PantryItem).where(PantryItem.household_id == household_id)):
        if is_available(item, day):
            items.setdefault(key_for(item.name), []).append(item)
    return Stock(items)


def _rows_by_key(db: Session, household_id: int) -> dict[str, list[PantryItem]]:
    """Every row, with what goes off first first, as that is what gets used first."""
    rows: dict[str, list[PantryItem]] = {}
    statement = select(PantryItem).where(PantryItem.household_id == household_id).order_by(PantryItem.expires_on, PantryItem.id)
    for item in db.scalars(statement):
        rows.setdefault(key_for(item.name), []).append(item)
    for items in rows.values():
        # Dates sort differently from one database to the next when some are missing.
        items.sort(key=lambda i: (i.expires_on is None, i.expires_on or date.max, i.id))
    return rows


def bought_amount(item: ShoppingItem) -> tuple[float | None, str | None]:
    """What came home. Whole packs do, and what the recipe leaves over is what might go to waste."""
    if item.quantity is not None and item.pack_quantity and item.packs and not item.manual:
        return max(item.quantity, item.packs * item.pack_quantity), item.unit
    if item.quantity is not None:
        return item.quantity, item.unit
    ingredient = find_ingredient(item.name)
    # "Mælk", typed in without an amount, is a carton. The amount has to be known for it to run out.
    return (ingredient.pack, ingredient.unit) if ingredient else (None, item.unit)


def _estimated_expiry(name: str, day: date) -> date | None:
    days = keeps_days(name)
    return day + timedelta(days=days) if days is not None else None


def add_bought(db: Session, household_id: int, name: str, quantity: float | None, unit: str | None, day: date, estimate_expiry: bool) -> None:
    """Puts something bought in the pantry, on top of what is there."""
    rows = _rows_by_key(db, household_id).get(key_for(name), [])
    estimate = _estimated_expiry(name, day) if estimate_expiry else None
    if not rows:
        db.add(
            PantryItem(
                household_id=household_id,
                name=name,
                quantity=quantity,
                unit=unit,
                source="shopping",
                expires_on=estimate,
                expires_estimated=estimate is not None,
            )
        )
        db.flush()
        return
    item = rows[0]
    was_empty = item.quantity is not None and item.quantity <= _EMPTY
    if item.quantity is not None and quantity is not None:
        added, base = to_base_unit(quantity, unit)
        factor, own_base = to_base_unit(1, item.unit)
        own_base = own_base or "stk"
        base = base or "stk"
        if base == own_base or {base, own_base} == {"g", "ml"}:
            item.quantity = round(item.quantity + (added or 0) / (factor or 1), 3)
    # A newer pack does not change when the older one goes off, unless that one is finished.
    if estimate is not None and (was_empty or item.expires_on is None or item.expires_on < day):
        item.expires_on, item.expires_estimated = estimate, True
    db.flush()


def consume_meal(db: Session, meal: PlannedMeal, child_portion: float) -> list[dict]:
    """Takes what a meal used out of the pantry, and remembers what, so that it can go back."""
    if meal.consumed_at is not None:
        return meal.consumed or []
    log: list[dict] = []
    recipe = meal.recipe
    if recipe is not None:
        rows = _rows_by_key(db, meal.plan.household_id)
        scale = meal_portions(meal, child_portion) / max(recipe.servings or 1, 0.5)
        for line in recipe.ingredients or []:
            raw = line.get("quantity")
            needed, unit = to_base_unit(raw * scale if raw is not None else None, line.get("unit"))
            name = line.get("name", "")
            if needed is None or not name:
                continue
            ingredient = find_ingredient(name)
            for item in rows.get(key_for(name), []):
                if needed <= 0:
                    break
                have = amount_in(item, unit, ingredient)
                if have is None or have <= _EMPTY:
                    continue
                taken = min(have, needed)
                needed -= taken
                before = item.quantity
                item.quantity = round(before * (have - taken) / have, 3)
                entry = {
                    "id": item.id,
                    "name": item.name,
                    "taken": round(before - item.quantity, 3),
                    "unit": item.unit,
                    "location": item.location,
                    "expires_on": item.expires_on.isoformat() if item.expires_on else None,
                    "expires_estimated": item.expires_estimated,
                    "min_quantity": item.min_quantity,
                    "source": item.source,
                }
                log.append(entry)
                # A row at zero is only worth keeping if someone wants to be told to buy more.
                if item.quantity <= _EMPTY and item.min_quantity is None:
                    db.delete(item)
    meal.consumed = log
    meal.consumed_at = utcnow()
    db.flush()
    return log


def restore_meal(db: Session, meal: PlannedMeal) -> None:
    """Puts back what a meal took, for when it turns out that it was not made after all."""
    if meal.consumed_at is None:
        return
    for entry in meal.consumed or []:
        item = db.get(PantryItem, entry["id"])
        if item is None or item.household_id != meal.plan.household_id:
            expires = entry.get("expires_on")
            item = PantryItem(
                household_id=meal.plan.household_id,
                name=entry["name"],
                quantity=0,
                unit=entry.get("unit"),
                location=entry.get("location"),
                expires_on=date.fromisoformat(expires) if expires else None,
                expires_estimated=bool(entry.get("expires_estimated")),
                min_quantity=entry.get("min_quantity"),
                source=entry.get("source") or "manual",
            )
            db.add(item)
        if item.quantity is not None:
            item.quantity = round(item.quantity + entry["taken"], 3)
    meal.consumed, meal.consumed_at = None, None
    db.flush()


def set_cooked(db: Session, meal: PlannedMeal, cooked: bool) -> None:
    """Someone says whether a meal was made. It settles what the pantry should say."""
    meal.cooked = cooked
    if cooked:
        hs = household_settings(db.get(Household, meal.plan.household_id).settings)
        if hs.track_consumption:
            consume_meal(db, meal, hs.child_portion)
    else:
        restore_meal(db, meal)
    db.flush()


def _was_eaten(meal: PlannedMeal, list_completed: bool) -> bool:
    """Accepted meals are assumed eaten once the day has passed. Meals nobody voted on count too,
    if the food for them was bought."""
    if meal.status == "rejected" or meal.cooked is False:
        return False
    return meal.status == "accepted" or meal.cooked is True or list_completed


def consume_past_meals(db: Session, today: date | None = None) -> int:
    """Daily job. Yesterday's dinner has been eaten, whether or not anybody said so."""
    today = today or local_today()
    since = today - timedelta(days=_CONSUME_LOOKBACK_DAYS)
    rows = db.execute(
        select(PlannedMeal, ShoppingList.completed_at, MealPlan.household_id)
        .join(MealPlan, MealPlan.id == PlannedMeal.plan_id)
        .outerjoin(ShoppingList, ShoppingList.plan_id == MealPlan.id)
        .where(
            PlannedMeal.date < today,
            PlannedMeal.date >= since,
            PlannedMeal.consumed_at.is_(None),
            PlannedMeal.status != "rejected",
            or_(PlannedMeal.cooked.is_(None), PlannedMeal.cooked.is_(True)),
        )
        .order_by(PlannedMeal.date)
    ).all()
    settings_of: dict[int, object] = {}
    consumed = 0
    for meal, completed_at, household_id in rows:
        if household_id not in settings_of:
            settings_of[household_id] = household_settings(db.get(Household, household_id).settings)
        hs = settings_of[household_id]
        if hs.track_consumption and _was_eaten(meal, completed_at is not None):
            consume_meal(db, meal, hs.child_portion)
            consumed += 1
    return consumed


def catch_up_after_shopping(db: Session, plan: MealPlan, child_portion: float, today: date) -> None:
    """The trip was registered late, so meals already eaten found nothing to take. Now they can."""
    for meal in plan.meals:
        if meal.date >= today or not _was_eaten(meal, True):
            continue
        if meal.consumed_at is not None and not meal.consumed:
            meal.consumed_at = None
        consume_meal(db, meal, child_portion)


def forget_old_estimates(db: Session, today: date | None = None) -> int:
    """Clears out what passed an estimated date long ago. A date from the package stays, as the
    person who typed it in is the one to decide what became of it."""
    cutoff = (today or local_today()) - timedelta(days=_FORGET_ESTIMATED_AFTER_DAYS)
    result = db.execute(
        delete(PantryItem).where(
            PantryItem.expires_estimated.is_(True),
            PantryItem.expires_on < cutoff,
            PantryItem.min_quantity.is_(None),
        )
    )
    return result.rowcount or 0


def job_pantry(db: Session) -> None:
    consume_past_meals(db)
    forget_old_estimates(db)
