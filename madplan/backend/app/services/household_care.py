"""The food-waste agent and the diet coach. Both work from facts, without needing AI."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.db import local_today
from app.models import Household, MealPlan, PantryItem, PlannedMeal, ShoppingItem, ShoppingList, User
from app.services.notifications import notify
from app.services.pricing import key_for
from app.services.recipes import allergy_conflict, diet_allows, recipe_traits, visible_recipes
from app.settings_schema import effective_profile, household_settings, user_settings


def expiring_items(db: Session, household_id: int, within_days: int, today: date | None = None) -> list[PantryItem]:
    cutoff = (today or local_today()) + timedelta(days=within_days)
    return list(
        db.scalars(
            select(PantryItem)
            .where(
                PantryItem.household_id == household_id,
                PantryItem.expires_on.is_not(None),
                PantryItem.expires_on <= cutoff,
                or_(PantryItem.quantity.is_(None), PantryItem.quantity > 0),
            )
            .order_by(PantryItem.expires_on)
        )
    )


def waste_suggestions(db: Session, household: Household, today: date | None = None) -> dict:
    """What is about to go off, and the dishes that use the most of it."""
    today = today or local_today()
    hs = household_settings(household.settings)
    items = expiring_items(db, household.id, hs.waste_alert_days, today)
    # Food already past its date is not something to plan dinner around.
    keys = {key_for(i.name): i for i in items if i.expires_on >= today}
    members = db.scalars(select(User).where(User.household_id == household.id, User.is_active.is_(True))).all()
    profile = effective_profile(household.settings, [m.settings for m in members])

    dishes = []
    if keys:
        for recipe in visible_recipes(db, household.id):
            if allergy_conflict(recipe, profile.allergies) or not diet_allows(profile.diet_mode, recipe):
                continue
            used = [line.get("name", "") for line in recipe.ingredients or [] if key_for(line.get("name", "")) in keys]
            if used:
                dishes.append({"recipe_id": recipe.id, "title": recipe.title, "title_en": recipe.title_en, "uses": used, "meal_types": recipe.meal_types})
        dishes.sort(key=lambda d: -len(d["uses"]))

    return {
        "items": [
            {
                "id": i.id,
                "name": i.name,
                "quantity": i.quantity,
                "unit": i.unit,
                "expires_on": i.expires_on.isoformat(),
                "days_left": (i.expires_on - today).days,
                "estimated": i.expires_estimated,
            }
            for i in items
        ],
        "dishes": dishes[:5],
    }


def run_waste_check(db: Session, today: date | None = None) -> int:
    """Daily job. Each user hears about a given batch of expiring items once."""
    today = today or local_today()
    sent = 0
    for household in db.scalars(select(Household)):
        hs = household_settings(household.settings)
        items = expiring_items(db, household.id, hs.waste_alert_days, today)
        if not items:
            continue
        names = ", ".join(i.name for i in items[:5])
        data = {"items": names, "count": len(items)}
        dishes = waste_suggestions(db, household, today)["dishes"]
        fresh = [i for i in items if i.expires_on >= today]
        if dishes and fresh:
            data.update(dish=dishes[0]["title"], dish_en=dishes[0]["title_en"], days_left=(fresh[0].expires_on - today).days)
        for user in db.scalars(select(User).where(User.household_id == household.id, User.is_active.is_(True))):
            if not user_settings(user.settings).features.waste_agent:
                continue
            key = f"waste:{today.isoformat()}"
            if notify(db, user, "waste", data, dedupe_key=key):
                sent += 1
    db.commit()
    return sent


_RED_MEAT = {"beef", "pork", "lamb"}


def diet_report(db: Session, household: Household, days: int = 28, today: date | None = None) -> dict:
    """Facts about what was actually eaten (accepted meals up to today) and bought."""
    today = today or local_today()
    since = today - timedelta(days=days)
    members = db.scalars(select(User).where(User.household_id == household.id, User.is_active.is_(True))).all()
    profile = effective_profile(household.settings, [m.settings for m in members])

    meals = list(
        db.scalars(
            select(PlannedMeal)
            .join(MealPlan, MealPlan.id == PlannedMeal.plan_id)
            .where(
                MealPlan.household_id == household.id,
                or_(PlannedMeal.status == "accepted", PlannedMeal.cooked.is_(True)),
                or_(PlannedMeal.cooked.is_(None), PlannedMeal.cooked.is_(True)),
                PlannedMeal.date >= since,
                PlannedMeal.date <= today,
            )
        )
    )
    proteins: Counter = Counter()
    vegetarian = fish = red_meat = on_diet = 0
    for meal in meals:
        if meal.recipe is None:
            continue
        traits = recipe_traits(meal.recipe)
        proteins.update(traits.proteins)
        if not traits.has_meat and not traits.has_fish:
            vegetarian += 1
        if traits.has_fish:
            fish += 1
        if traits.proteins & _RED_MEAT:
            red_meat += 1
        if diet_allows(profile.diet_mode, meal.recipe, traits):
            on_diet += 1

    bought = db.scalar(
        select(ShoppingList.id).where(ShoppingList.household_id == household.id, ShoppingList.completed_at.is_not(None)).limit(1)
    )
    bought_count = 0
    if bought:
        bought_count = len(
            db.scalars(
                select(ShoppingItem.id)
                .join(ShoppingList, ShoppingItem.list_id == ShoppingList.id)
                .where(ShoppingList.household_id == household.id, ShoppingItem.checked.is_(True))
            ).all()
        )

    total = len(meals)
    suggestions: list[dict] = []
    if total >= 3:
        if red_meat / total > 0.5:
            suggestions.append({"code": "less_red_meat", "share": round(red_meat / total, 2)})
        if fish / total < 0.15:
            suggestions.append({"code": "more_fish", "share": round(fish / total, 2)})
        if vegetarian / total < 0.15:
            suggestions.append({"code": "more_vegetarian", "share": round(vegetarian / total, 2)})
        if profile.diet_mode and on_diet < total:
            suggestions.append({"code": "off_diet_meals", "count": total - on_diet, "diet": profile.diet_mode})

    return {
        "days": days,
        "diet_mode": profile.diet_mode,
        "meals": total,
        "items_bought": bought_count,
        "proteins": dict(proteins),
        "vegetarian_meals": vegetarian,
        "fish_meals": fish,
        "red_meat_meals": red_meat,
        "on_diet_meals": on_diet,
        "suggestions": suggestions,
    }
