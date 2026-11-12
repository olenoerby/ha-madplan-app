"""Turns a plan into a shopping list for the whole trip, and works out where to buy it.

Quantities are added up across the week before packs are counted, so two dishes that each use
half a pack of mince buy one pack. The list total is what the trip costs at the till, which is
why it can be higher than the sum of the per-meal shares.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import local_today, utcnow
from app.models import Expense, Household, MealPlan, ShoppingItem, ShoppingList, User
from app.services.pantry import add_bought, bought_amount, catch_up_after_shopping, load_stock
from app.services.pricing import PriceBook, Quote, is_staple, key_for, load_offer_views
from app.services.recipes import household_profile, meal_portions
from app.services.stores import StoreChoice, household_store_choices
from app.services.text import round_quantity, to_base_unit
from app.settings_schema import HouseholdSettings, household_settings


@dataclass
class Line:
    name: str
    quantity: float | None
    unit: str | None
    staple: bool = False


def aggregate_lines(plan: MealPlan, child_portion: float, from_day: date | None = None) -> dict[tuple[str, str | None], Line]:
    """Adds up the ingredients of what is still to be made, in base units.

    Monday's dinner has nothing to do with a list that is being redone on Wednesday.
    """
    totals: dict[tuple[str, str | None], Line] = {}
    for meal in plan.meals:
        if meal.status == "rejected" or meal.recipe is None or meal.cooked is False or meal.consumed_at is not None:
            continue
        if from_day is not None and meal.date < from_day:
            continue
        people = meal_portions(meal, child_portion)
        scale = people / max(meal.recipe.servings or 1, 0.5)
        for line in meal.recipe.ingredients or []:
            name = line.get("name", "")
            if not name:
                continue
            raw_quantity = line.get("quantity")
            quantity, unit = to_base_unit(
                raw_quantity * scale if raw_quantity is not None else None, line.get("unit")
            )
            key = (key_for(name), unit)
            current = totals.get(key)
            if current is None:
                totals[key] = Line(name, quantity, unit, staple=is_staple(name))
            elif quantity is not None:
                current.quantity = (current.quantity or 0) + quantity
    return totals


@dataclass
class StoreOption:
    store_ids: tuple[int, ...]
    goods: float
    travel: float
    extra_stop_cost: float
    total: float
    missing_distance: bool
    covered_lines: int


def _travel_km(distances: list[float]) -> float:
    """A loop from home: there and back to the farthest, plus a detour for each other stop."""
    if not distances:
        return 0.0
    farthest = max(distances)
    return 2 * farthest + sum(distances) - farthest


def optimize_stores(
    lines: list[Line],
    book: PriceBook,
    choices: list[StoreChoice],
    hs: HouseholdSettings,
) -> tuple[StoreOption | None, list[StoreOption], dict[int, dict[int, Quote]]]:
    """Tries every combination up to max_stores and weighs the prices against the driving.

    Items no chosen store lists are priced from the estimate, the same for every combination,
    so they do not favour one store over another.
    """
    enabled = [c for c in choices if c.enabled]
    quotes = {i: book.per_store(line.name, line.quantity, line.unit) for i, line in enumerate(lines)}
    fallback = {i: book.fallback(line.name, line.quantity, line.unit) for i, line in enumerate(lines)}
    by_id = {c.store.id: c for c in enabled}
    relevant = [sid for sid in by_id if any(sid in q for q in quotes.values())]
    # With no prices anywhere there is still something useful to say: go to the nearest store.
    if not relevant:
        relevant = list(by_id)[: hs.max_stores]

    options: list[StoreOption] = []
    for size in range(1, hs.max_stores + 1):
        for combo in itertools.combinations(relevant, size):
            goods = 0.0
            covered = 0
            for i in quotes:
                prices = [quotes[i][sid].cost for sid in combo if sid in quotes[i] and quotes[i][sid].cost is not None]
                if prices:
                    goods += min(prices)
                    covered += 1
                elif fallback[i].cost is not None:
                    goods += fallback[i].cost
            distances = [by_id[sid].distance_km for sid in combo if by_id[sid].distance_km is not None]
            travel = _travel_km(distances) * hs.cost_per_km
            extra = (size - 1) * hs.cost_per_extra_store
            options.append(
                StoreOption(
                    store_ids=combo,
                    goods=round(goods, 2),
                    travel=round(travel, 2),
                    extra_stop_cost=round(extra, 2),
                    total=round(goods + travel + extra, 2),
                    missing_distance=len(distances) < size,
                    covered_lines=covered,
                )
            )
    options.sort(key=lambda o: (o.total, len(o.store_ids)))
    return (options[0] if options else None), options, quotes


def build_shopping_list(db: Session, plan: MealPlan, user: User | None = None, book: PriceBook | None = None) -> ShoppingList:
    """Creates or refreshes the plan's list. Manual additions and ticked items are preserved.

    A finished list is left as it is: it is the record of a trip, and what it bought is in the
    pantry by now, so redoing it would show everything as already at home.

    Pass ``book`` when building repeatedly, so the offers are only loaded and indexed once.
    """
    household = db.get(Household, plan.household_id)
    hs = household_settings(household.settings)
    db.flush()
    db.refresh(plan, attribute_names=["meals"])

    shopping = db.scalar(select(ShoppingList).where(ShoppingList.plan_id == plan.id))
    if shopping is None:
        shopping = ShoppingList(
            household_id=plan.household_id,
            plan_id=plan.id,
            name=f"{plan.start_date.isoformat()} – {plan.end_date.isoformat()}",
        )
        db.add(shopping)
        db.flush()
    elif shopping.completed_at is not None:
        return shopping

    checked_names = {item.name for item in shopping.items if item.checked and not item.manual}
    for item in [i for i in shopping.items if not i.manual]:
        shopping.items.remove(item)

    shopping_day = max(plan.start_date, local_today())
    choices = household_store_choices(db, household, user)
    enabled_ids = {c.store.id for c in choices if c.enabled}

    if book is None:
        profile = household_profile(db, household)
        book = PriceBook(load_offer_views(db, enabled_ids, shopping_day), profile.allergies, hs.blocked_matches)
    stock = load_stock(db, plan.household_id, shopping_day)

    lines = aggregate_lines(plan, hs.child_portion, local_today())
    to_buy: list[Line] = []
    covered: list[Line] = []
    for (key, unit), line in lines.items():
        if line.staple and hs.assume_staples:
            covered.append(line)
            continue
        if not stock.has(key):
            to_buy.append(line)
            continue
        remaining = stock.amount(key, unit)
        if remaining is None or line.quantity is None or remaining >= line.quantity:
            # The pantry says we have it, and if the amount is unknown we take its word for it.
            covered.append(line)
        else:
            covered.append(Line(line.name, remaining, unit))
            to_buy.append(Line(line.name, line.quantity - remaining, unit))

    best, options, quotes = optimize_stores(to_buy, book, choices, hs)
    chosen_ids = set(best.store_ids) if best else set()

    by_store: dict[str, dict] = {}
    total = normal_total = saving_total = saving_measured = 0.0
    real_prices = estimated = unknown = 0
    for index, line in enumerate(to_buy):
        options_here = {sid: q for sid, q in quotes[index].items() if sid in chosen_ids and q.cost is not None}
        quote = min(options_here.values(), key=lambda q: q.cost) if options_here else book.fallback(line.name, line.quantity, line.unit)
        shopping.items.append(
            ShoppingItem(
                name=line.name,
                quantity=round_quantity(line.quantity, line.unit) if line.quantity is not None else None,
                unit=line.unit,
                packs=quote.packs,
                store_id=quote.store_id,
                offer_id=quote.offer_id,
                offer_title=quote.offer_title,
                price_source=quote.price_source,
                cost=quote.cost,
                normal_cost=quote.normal_cost,
                normal_source=quote.normal_source,
                pack_quantity=quote.pack_quantity,
                checked=line.name in checked_names,
            )
        )
        if quote.cost is None:
            unknown += 1
            continue
        total += quote.cost
        if quote.normal_cost is not None:
            normal_total += quote.normal_cost
            saving_total += quote.saving
            if quote.normal_source in ("before", "shelf"):
                saving_measured += quote.saving
        if quote.price_source in ("offer", "shelf"):
            real_prices += 1
        else:
            estimated += 1
        if quote.store_name:
            entry = by_store.setdefault(quote.store_name, {"store": quote.store_name, "cost": 0.0, "items": 0})
            entry["cost"] = round(entry["cost"] + quote.cost, 2)
            entry["items"] += 1

    for line in covered:
        shopping.items.append(
            ShoppingItem(
                name=line.name,
                quantity=round_quantity(line.quantity, line.unit) if line.quantity is not None else None,
                unit=line.unit,
                covered_by_pantry=True,
                price_source="unknown",
                cost=0,
                normal_cost=0,
            )
        )

    names = {c.store.id: c.store.name for c in choices}
    single_options = [o for o in options if len(o.store_ids) == 1]
    best_single = single_options[0] if single_options else None

    def describe(option: StoreOption) -> dict:
        return {
            "stores": [names.get(sid, "?") for sid in option.store_ids],
            "goods": option.goods,
            "travel": option.travel,
            "extra_stop_cost": option.extra_stop_cost,
            "total": option.total,
            "missing_distance": option.missing_distance,
            "lines_with_price": option.covered_lines,
        }

    budget = plan.budget
    shopping.summary = {
        "total": round(total, 2),
        "normal_total": round(normal_total, 2),
        "savings": round(saving_total, 2),
        # The part measured against prices stores really charge, and not against our own estimate.
        "savings_measured": round(saving_measured, 2),
        "budget": budget,
        "within_budget": None if not budget else total <= budget,
        "real_prices": real_prices,
        "estimated_prices": estimated,
        "unknown_prices": unknown,
        "by_store": sorted(by_store.values(), key=lambda s: -s["cost"]),
        "store_plan": describe(best) if best else None,
        "best_single_store": describe(best_single) if best_single else None,
        "extra_store_saves": round(best_single.total - best.total, 2) if best and best_single else 0,
        "store_alternatives": [describe(o) for o in options[:4]],
    }
    db.flush()
    return shopping


def apply_receipt(db: Session, shopping: ShoppingList, lines: list[dict]) -> None:
    """What a person confirmed from a photo of the receipt: real prices, and what was really bought.

    A line matched to an item on the list ticks it off with the price paid. A line with no item
    is added, as it was bought too, and so goes into the pantry with the rest.
    """
    items = {i.id: i for i in shopping.items}
    for line in lines:
        item = items.get(line.get("item_id")) if line.get("item_id") is not None else None
        price = round(float(line["price"]), 2)
        if item is None:
            item = ShoppingItem(
                list_id=shopping.id,
                name=str(line["name"])[:200],
                quantity=line.get("quantity"),
                unit=line.get("unit"),
                packs=1,
                manual=True,
            )
            shopping.items.append(item)
        item.checked = True
        item.covered_by_pantry = False
        item.cost = price
        item.price_source = "receipt"
    db.flush()


def complete_shopping_list(
    db: Session, shopping: ShoppingList, user: User, amount: float | None, day: date | None = None, receipt: list[dict] | None = None
) -> Expense:
    """Records the trip as spending and moves what was bought into the pantry.

    ``amount`` is what the receipt said, when someone typed it in or a photo of it was read.
    Otherwise the list's estimate of the ticked items is used, and the note says so. ``receipt`` is
    the lines a person confirmed from a photo of the receipt.
    """
    if receipt:
        apply_receipt(db, shopping, receipt)
    bought = [i for i in shopping.items if i.checked and not i.covered_by_pantry]
    estimate = round(sum(i.cost or 0 for i in bought), 2)
    savings = round(sum(max(0.0, (i.normal_cost or 0) - (i.cost or 0)) for i in bought if i.normal_cost), 2)
    expense = Expense(
        household_id=shopping.household_id,
        user_id=user.id,
        day=day or local_today(),
        amount=round(amount, 2) if amount is not None else estimate,
        savings=savings,
        list_id=shopping.id,
        note=("receipt_photo" if receipt else "receipt") if amount is not None else ("receipt_lines" if receipt else "estimate"),
    )
    db.add(expense)
    hs = household_settings(db.get(Household, shopping.household_id).settings)
    trip_day = day or local_today()
    for item in bought:
        quantity, unit = bought_amount(item)
        add_bought(db, shopping.household_id, item.name, quantity, unit, trip_day, hs.estimate_expiry)
    shopping.completed_at = utcnow()
    if shopping.plan_id:
        # What the plan was shopping for is bought now, so it is no longer voted off.
        for meal in db.get(MealPlan, shopping.plan_id).meals:
            if meal.status != "rejected":
                meal.bought = "full"
    db.flush()
    if shopping.plan_id and hs.track_consumption:
        catch_up_after_shopping(db, db.get(MealPlan, shopping.plan_id), hs.child_portion, local_today())
    return expense


def open_list(db: Session, household_id: int) -> ShoppingList | None:
    """The list things are being added to: the newest that has not been shopped yet."""
    return db.scalar(
        select(ShoppingList)
        .where(ShoppingList.household_id == household_id, ShoppingList.completed_at.is_(None))
        .order_by(ShoppingList.id.desc())
        .limit(1)
    )


def add_manual_item(db: Session, household_id: int, name: str, quantity: float | None = None, unit: str | None = None, list_name: str = "Løbende indkøb") -> ShoppingItem:
    """Puts something on the current list, starting one if there is none."""
    shopping = open_list(db, household_id)
    if shopping is None:
        shopping = ShoppingList(household_id=household_id, name=list_name)
        db.add(shopping)
        db.flush()
    item = ShoppingItem(list_id=shopping.id, name=name, quantity=quantity, unit=unit, manual=True)
    db.add(item)
    db.flush()
    return item
