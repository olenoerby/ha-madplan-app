"""Builds a meal plan from recipes, real prices, the pantry and what people want.

Everything here is deterministic given its inputs, so a plan can always be made without AI.
The AI's contribution is extra candidate recipes, which go through exactly the same filters:
an allergen cannot get into a plan by way of a suggestion from a model.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.db import local_today
from app.models import Household, MealPlan, PlanDay, PlannedMeal, Recipe, ShoppingList, User
from app.services.favorites import LearnedTaste, learn_taste
from app.services.leftovers import drop_leftovers, forget_source, retitle
from app.services.notifications import notify
from app.services.pantry import Stock, load_stock
from app.services.pricing import PriceBook, is_staple, key_for, load_offer_views
from app.services.recipes import (
    allergy_conflict,
    diet_allows,
    diet_bonus,
    eligible_recipes,
    household_profile,
    meal_portions,
    portions,
    recipe_traits,
    term_hits_recipe,
    visible_recipes,
)
from app.services.shopping import build_shopping_list
from app.services.stores import enabled_store_ids
from app.services.text import normalize, text_matches, to_base_unit, words
from app.services.voting import ensure_can_drop
from app.settings_schema import EffectiveProfile, HouseholdSettings, household_settings, user_settings


class NoRecipes(ValueError):
    """There is nothing to plan from: no real recipes that this household can eat."""


# A change in what the shopping trip costs that is worth telling people about.
_NOTABLE_CHANGE_KR = 25.0
_NOTABLE_CHANGE_SHARE = 0.05
# Less than this of what a dish needs is a leftover, and no reason to choose the dish.
_USEFUL_SHARE = 0.25

MODES = ("days_ahead", "full_week", "workweek", "range")
MAX_BUDGET_SWAPS = 12


def resolve_dates(
    mode: str,
    today: date | None = None,
    days_ahead: int = 7,
    start: date | None = None,
    end: date | None = None,
) -> list[date]:
    """Turns the chosen period into dates. "Week" means the next Monday unless a start is given."""
    today = today or local_today()
    if mode == "days_ahead":
        first = start or today
        return [first + timedelta(days=i) for i in range(max(1, days_ahead))]
    if mode in ("full_week", "workweek"):
        monday = start or today + timedelta(days=(7 - today.weekday()) % 7)
        monday -= timedelta(days=monday.weekday())
        return [monday + timedelta(days=i) for i in range(7 if mode == "full_week" else 5)]
    if mode == "range":
        if not start or not end or end < start:
            raise ValueError("a range needs a start and an end on or after it")
        if (end - start).days > 31:
            raise ValueError("a plan can span at most 32 days")
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]
    raise ValueError(f"unknown mode {mode!r}")


@dataclass
class Scored:
    recipe: Recipe
    score: float
    cost: float
    normal_cost: float | None
    reasons: list[dict]
    proteins: frozenset[str]


@dataclass
class PlanContext:
    household: Household
    settings: HouseholdSettings
    profile: EffectiveProfile
    book: PriceBook
    stock: Stock
    expiring_keys: set[str]
    taste: LearnedTaste
    last_served: dict[str, date]
    max_price_per_meal: float | None = None
    rng: random.Random = field(default_factory=random.Random)
    cost_cache: dict[tuple[int, float], tuple] = field(default_factory=dict)


def recently_served(db: Session, household_id: int, before: date, days: int) -> dict[str, date]:
    """The last date each dish was planned and not rejected, keyed by its normalized title."""
    if days <= 0:
        return {}
    rows = db.execute(
        select(PlannedMeal.title, PlannedMeal.date)
        .join(MealPlan, MealPlan.id == PlannedMeal.plan_id)
        .where(
            MealPlan.household_id == household_id,
            PlannedMeal.date >= before - timedelta(days=days),
            PlannedMeal.date < before,
            PlannedMeal.status != "rejected",
            or_(PlannedMeal.cooked.is_(None), PlannedMeal.cooked.is_(True)),
        )
    ).all()
    latest: dict[str, date] = {}
    for title, day in rows:
        key = normalize(title)
        if key not in latest or day > latest[key]:
            latest[key] = day
    return latest


def build_context(
    db: Session,
    household: Household,
    shopping_day: date,
    user: User | None = None,
    seed: int | None = None,
) -> PlanContext:
    hs = household_settings(household.settings)
    profile = household_profile(db, household)
    offers = load_offer_views(db, enabled_store_ids(db, household, user), shopping_day)
    stock = load_stock(db, household.id, shopping_day)
    expiry_cutoff = shopping_day + timedelta(days=hs.waste_alert_days)
    expiring = {
        key for key, items in stock.items.items() if any(i.expires_on and i.expires_on <= expiry_cutoff for i in items)
    }
    max_price = user_settings(user.settings).max_price_per_meal if user else None
    return PlanContext(
        household=household,
        settings=hs,
        profile=profile,
        book=PriceBook(offers, profile.allergies, hs.blocked_matches),
        stock=stock,
        expiring_keys=expiring,
        taste=learn_taste(db, household.id),
        last_served=recently_served(db, household.id, shopping_day, hs.repeat_avoid_days),
        max_price_per_meal=max_price,
        rng=random.Random(seed),  # noqa: S311 (variety between plans, nothing secret)
    )


def _cost_recipe(recipe: Recipe, people: float, ctx: PlanContext) -> tuple[float, float | None, list[str], list[str], list[str]]:
    """Cost of the portion used; the shopping list later buys whole packs across the week."""
    cached = ctx.cost_cache.get((recipe.id, people))
    if cached is not None:
        return cached
    scale = people / max(recipe.servings or 1, 0.5)
    cost = 0.0
    normal = 0.0
    any_normal = False
    on_offer: list[str] = []
    from_pantry: list[str] = []
    expiring: list[str] = []
    for line in recipe.ingredients or []:
        name = line.get("name", "")
        if ctx.settings.assume_staples and is_staple(name):
            continue
        key = key_for(name)
        quantity = line.get("quantity")
        needed, unit = to_base_unit(quantity * scale if quantity is not None else None, line.get("unit"))
        if ctx.stock.has(key):
            have = ctx.stock.amount(key, unit)
            if have is None or needed is None or have >= needed * _USEFUL_SHARE:
                from_pantry.append(name)
                if key in ctx.expiring_keys:
                    expiring.append(name)
            if have is None or needed is None or have >= needed:
                continue
            # Only the rest has to be bought. A leftover does not make the whole dish free.
            needed -= have
        quote = ctx.book.quote(name, needed, unit)
        if quote.cost is None:
            continue
        share = 1.0
        if quote.pack_quantity and quote.quantity and quote.packs:
            share = min(1.0, quote.quantity / (quote.pack_quantity * quote.packs))
        cost += quote.cost * share
        if quote.normal_cost is not None:
            normal += quote.normal_cost * share
            any_normal = True
        if quote.price_source == "offer" and quote.saving > 0:
            on_offer.append(name)
    result = (round(cost, 2), (round(normal, 2) if any_normal else None), on_offer, from_pantry, expiring)
    ctx.cost_cache[(recipe.id, people)] = result
    return result


def score_recipe(recipe: Recipe, people: float, day: date, ctx: PlanContext, week_proteins: Counter, previous: frozenset[str]) -> Scored:
    traits = recipe_traits(recipe)
    cost, normal, on_offer, from_pantry, expiring = _cost_recipe(recipe, people, ctx)
    score = 0.0
    reasons: list[dict] = []

    if normal and normal > cost:
        saving = normal - cost
        score += min(30.0, saving / normal * 40)
        if on_offer:
            reasons.append({"code": "on_offer", "items": on_offer[:4], "saving": round(saving, 2)})
    # Modest, so that a like can outweigh a few kroner. fit_to_budget deals with the total.
    score -= cost / max(people, 1) * 0.15
    score += min(len(on_offer), 4) * 1.5

    if from_pantry:
        score += min(len(from_pantry), 4) * 3
        reasons.append({"code": "uses_pantry", "items": from_pantry[:4]})
    if expiring:
        score += len(expiring) * 12
        reasons.append({"code": "uses_expiring", "items": expiring[:4]})

    for term in ctx.profile.likes:
        if term_hits_recipe(term, recipe):
            score += 10
            reasons.append({"code": "liked", "term": term})
    for term in ctx.profile.meat_preferences:
        if term_hits_recipe(term, recipe):
            score += 8
            reasons.append({"code": "preferred_meat", "term": term})
    for term, wish in ctx.profile.frequencies.items():
        if not term_hits_recipe(term, recipe):
            continue
        if wish < 0:
            # "Rarely": once in a week is fine, a second time is not.
            score += wish * 10 - (40 if week_proteins[f"term:{term.lower()}"] >= 1 else 0)
            reasons.append({"code": "wanted_rarely", "term": term})
        elif wish > 0:
            score += wish * 12
            reasons.append({"code": "wanted_always" if wish >= 1.5 else "wanted_often", "term": term})
    for term in ctx.profile.dislikes:
        # Lower, not out: a dislike is a preference. Allergies are what excludes.
        if term_hits_recipe(term, recipe):
            score -= 18
            reasons.append({"code": "disliked", "term": term})

    for word in set(words(recipe.title)):
        weight = ctx.taste.weights.get(word)
        if weight:
            score += weight * 4
            if weight > 0.5:
                reasons.append({"code": "usually_yes", "term": word})
            elif weight < -0.5:
                reasons.append({"code": "usually_no", "term": word})

    mode = ctx.profile.diet_mode
    bonus = diet_bonus(mode, recipe, traits)
    if bonus > 0:
        reasons.append({"code": "diet_match", "diet": mode})
    score += bonus

    last = ctx.last_served.get(normalize(recipe.title))
    if last is not None:
        score -= 60
        reasons.append({"code": "repeated_recently", "days": (day - last).days})

    limit = ctx.settings.repeat_protein_per_week
    for protein in traits.proteins:
        if limit and week_proteins[protein] >= limit:
            score -= 25
    if traits.proteins & previous:
        score -= 8

    if ctx.max_price_per_meal and cost > ctx.max_price_per_meal:
        score -= 30

    # A little variation, so that regenerating gives a different plan among near-equals.
    score += ctx.rng.uniform(0, 3)
    return Scored(recipe, round(score, 2), cost, normal, reasons, traits.proteins)


@dataclass
class PlanRequest:
    dates: list[date]
    meal_types: list[str]
    skip_dates: set[date] = field(default_factory=set)
    # Meals Mealie already has a dish for. Mealie has the last word, so they are left to it.
    taken: set[tuple[date, str]] = field(default_factory=set)
    guests: dict[date, tuple[int, int]] = field(default_factory=dict)
    adults: int | None = None
    children: int | None = None
    budget: float | None = None
    seed: int | None = None


def _people(ctx: PlanContext, request: PlanRequest, day: date) -> tuple[int, int, float]:
    adults = request.adults if request.adults is not None else ctx.settings.default_adults
    children = request.children if request.children is not None else ctx.settings.default_children
    extra_adults, extra_children = request.guests.get(day, (0, 0))
    adults += extra_adults
    children += extra_children
    return adults, children, portions(adults, children, ctx.settings.child_portion)


def _choose(pool: list[Recipe], people: float, day: date, ctx: PlanContext, used: set[int], week_proteins: Counter, previous: frozenset[str], exclude: set[int] | None = None) -> list[Scored]:
    """Candidates best first, with dishes already in this plan pushed to the back."""
    scored = []
    for recipe in pool:
        if exclude and recipe.id in exclude:
            continue
        item = score_recipe(recipe, people, day, ctx, week_proteins, previous)
        if recipe.id in used:
            item.score -= 100
        scored.append(item)
    return sorted(scored, key=lambda s: -s.score)


def generate_plan(
    db: Session,
    household: Household,
    request: PlanRequest,
    user: User | None = None,
    extra_candidates: list[Recipe] | None = None,
) -> MealPlan:
    if not request.dates:
        raise ValueError("no dates to plan")
    shopping_day = max(min(request.dates), local_today())
    ctx = build_context(db, household, shopping_day, user, request.seed)
    recipes = visible_recipes(db, household.id)
    known = {r.id for r in recipes}
    recipes += [r for r in (extra_candidates or []) if r.id not in known]
    pools = {mt: eligible_recipes(recipes, mt, ctx.profile) for mt in request.meal_types}
    if not any(pools.values()):
        raise NoRecipes("no_recipes")

    skip_weekdays = set(ctx.settings.skip_weekdays)
    budget = request.budget if request.budget is not None else ctx.settings.budget_weekly
    plan = MealPlan(
        household_id=household.id,
        created_by=user.id if user else None,
        start_date=min(request.dates),
        end_date=max(request.dates),
        meal_types=list(request.meal_types),
        budget=budget,
        ai_used=bool(extra_candidates),
    )
    db.add(plan)
    db.flush()

    used: set[int] = set()
    week_proteins: Counter = Counter()
    previous: frozenset[str] = frozenset()

    for day in sorted(request.dates):
        skipped = day in request.skip_dates or day.weekday() in skip_weekdays
        guest_adults, guest_children = request.guests.get(day, (0, 0))
        db.add(PlanDay(plan_id=plan.id, date=day, skipped=skipped, guest_adults=guest_adults, guest_children=guest_children))
        if skipped:
            continue
        adults, children, people = _people(ctx, request, day)
        for meal_type in request.meal_types:
            if (day, meal_type) in request.taken:
                continue
            options = _choose(pools.get(meal_type, []), people, day, ctx, used, week_proteins, previous)
            if not options:
                continue
            best = options[0]
            # Better an empty evening that says so than the same dinner twice in one plan. A
            # repeated breakfast is just breakfast.
            if meal_type == "dinner" and best.recipe.id in used:
                continue
            db.add(_meal_from(plan, day, meal_type, best, adults, children))
            used.add(best.recipe.id)
            if meal_type == "dinner":
                for protein in best.proteins:
                    week_proteins[protein] += 1
                previous = best.proteins
            for term, wish in ctx.profile.frequencies.items():
                if wish < 0 and term_hits_recipe(term, best.recipe):
                    week_proteins[f"term:{term.lower()}"] += 1
    db.flush()
    db.refresh(plan)

    build_shopping_list(db, plan, user, ctx.book)
    if budget:
        fit_to_budget(db, plan, ctx, budget, pools, user)
    return plan


def missing_slots(plan: MealPlan) -> int:
    """Meals the plan was meant to have, but for which there was no recipe to suggest."""
    filled = {(m.date, m.meal_type) for m in plan.meals}
    return sum(1 for d in plan.days if not d.skipped for mt in plan.meal_types if (d.date, mt) not in filled)


def recipes_are_short(db: Session, household: Household, meal_types: list[str], days: int) -> bool:
    """Whether a plan of this length would have too little to choose from, and more is worth a search."""
    recipes = visible_recipes(db, household.id)
    profile = household_profile(db, household)
    return any(len(eligible_recipes(recipes, mt, profile)) < days * 2 for mt in meal_types)


def _meal_from(plan: MealPlan, day: date, meal_type: str, item: Scored, adults: int, children: int) -> PlannedMeal:
    return PlannedMeal(
        plan_id=plan.id,
        date=day,
        meal_type=meal_type,
        recipe_id=item.recipe.id,
        title=item.recipe.title,
        adults=adults,
        children=children,
        estimated_cost=item.cost,
        normal_cost=item.normal_cost,
        reasons=item.reasons,
        recipe=item.recipe,
    )


def _apply(meal: PlannedMeal, item: Scored) -> None:
    meal.recipe_id = item.recipe.id
    meal.recipe = item.recipe
    meal.title = item.recipe.title
    meal.estimated_cost = item.cost
    meal.normal_cost = item.normal_cost
    meal.reasons = item.reasons
    meal.status = "pending"
    meal.sync_status = "none"
    meal.sync_error = None


def fit_to_budget(db: Session, plan: MealPlan, ctx: PlanContext, budget: float, pools: dict[str, list[Recipe]], user: User | None) -> None:
    """Swaps the costliest meals for cheaper ones until the bill fits, if it can.

    Measured against the shopping list, since that is what is paid: whole packs for the week,
    which comes to more than the sum of what each meal uses. If it cannot be done, the list's
    summary says so, rather than the plan quietly claiming to fit.
    """
    for _ in range(MAX_BUDGET_SWAPS):
        if (build_shopping_list(db, plan, user, ctx.book).summary or {}).get("total", 0) <= budget:
            return
        used = {m.recipe_id for m in plan.meals if m.recipe_id}
        # The biggest saving that people will still be happy with. Going by the saving alone
        # would swap in what someone said they do not like, for the sake of a few kroner.
        best_swap: tuple[float, PlannedMeal, Scored] | None = None
        for meal in sorted(plan.meals, key=lambda m: -m.estimated_cost)[:6]:
            people = meal_portions(meal, ctx.settings.child_portion)
            for recipe in pools.get(meal.meal_type, []):
                if recipe.id in used:
                    continue
                item = score_recipe(recipe, people, meal.date, ctx, Counter(), frozenset())
                saving = meal.estimated_cost - item.cost
                worth = saving + item.score
                if saving > 0 and (best_swap is None or worth > best_swap[0]):
                    best_swap = (worth, meal, item)
        if best_swap is None:
            return
        _, meal, item = best_swap
        item.reasons.append({"code": "budget_swap", "replaced": meal.title})
        _apply(meal, item)
        db.flush()
    build_shopping_list(db, plan, user, ctx.book)


def replacement_options(db: Session, meal: PlannedMeal, user: User | None, limit: int = 3) -> list[Scored]:
    """The next best dishes for this slot, leaving out what is already planned or was refused."""
    plan = meal.plan
    household = db.get(Household, plan.household_id)
    ctx = build_context(db, household, max(plan.start_date, local_today()), user)
    pool = eligible_recipes(visible_recipes(db, household.id), meal.meal_type, ctx.profile)
    used = {m.recipe_id for m in plan.meals if m.recipe_id}
    people = meal_portions(meal, ctx.settings.child_portion)
    options = _choose(pool, people, meal.date, ctx, set(), Counter(), frozenset(), exclude=used)
    return options[:limit]


def replace_meal(db: Session, meal: PlannedMeal, recipe: Recipe, user: User | None) -> PlannedMeal:
    ensure_can_drop(meal)
    plan = meal.plan
    household = db.get(Household, plan.household_id)
    ctx = build_context(db, household, max(plan.start_date, local_today()), user)
    conflict = allergy_conflict(recipe, ctx.profile.allergies)
    if conflict:
        raise ValueError(f"allergy:{conflict}")
    people = meal_portions(meal, ctx.settings.child_portion)
    item = score_recipe(recipe, people, meal.date, ctx, Counter(), frozenset())
    _apply(meal, item)
    retitle(db, meal)
    meal.bought = "none"
    meal.added_by = None
    meal.votes.clear()
    db.flush()
    return meal


def rescale_meals_for_day(db: Session, plan: MealPlan, day: date, guest_adults: int, guest_children: int, user: User | None) -> list[dict]:
    """Applies guests to a day: amounts are recalculated, and a dish that gets expensive is flagged.

    Returns suggestions rather than swapping, because the people cooking may well want to keep
    the dish. A meal already accepted keeps its status, as the head count is the only change.
    """
    household = db.get(Household, plan.household_id)
    ctx = build_context(db, household, max(plan.start_date, local_today()), user)
    day_row = next((d for d in plan.days if d.date == day), None)
    if day_row is None:
        raise ValueError("that date is not in this plan")
    day_row.guest_adults, day_row.guest_children = guest_adults, guest_children

    suggestions: list[dict] = []
    base_adults = ctx.settings.default_adults
    base_children = ctx.settings.default_children
    for meal in [m for m in plan.meals if m.date == day and m.recipe]:
        meal.adults = base_adults + guest_adults
        meal.children = base_children + guest_children
        people = meal_portions(meal, ctx.settings.child_portion)
        previous_cost = meal.estimated_cost
        cost, normal, *_ = _cost_recipe(meal.recipe, people, ctx)
        meal.estimated_cost, meal.normal_cost = cost, normal
        if cost > previous_cost * 1.4 and cost - previous_cost > 40:
            cheaper = [o for o in replacement_options(db, meal, user, 5) if o.cost < cost * 0.8]
            if cheaper:
                suggestions.append(
                    {
                        "meal_id": meal.id,
                        "title": meal.title,
                        "new_cost": cost,
                        "alternatives": [
                            {"recipe_id": o.recipe.id, "title": o.recipe.title, "cost": o.cost} for o in cheaper[:3]
                        ],
                    }
                )
    db.flush()
    build_shopping_list(db, plan, user)
    return suggestions


def set_day_skipped(db: Session, plan: MealPlan, day: date, skipped: bool, user: User | None) -> None:
    """Skipping removes that day's meals. Un-skipping fills it again from the same pools."""
    day_row = next((d for d in plan.days if d.date == day), None)
    if day_row is None:
        raise ValueError("that date is not in this plan")
    if skipped:
        for meal in plan.meals:
            if meal.date == day:
                ensure_can_drop(meal)
    day_row.skipped = skipped
    if skipped:
        for meal in [m for m in plan.meals if m.date == day]:
            drop_leftovers(db, meal)
            forget_source(db, meal)
            if meal.mealie_entry_id or meal.mealie_note_id:
                # It is in Mealie, and comes out the next time someone sends the plan.
                meal.status = "rejected"
            else:
                plan.meals.remove(meal)
    else:
        for meal in [m for m in plan.meals if m.date == day and m.status == "rejected" and not m.mealie_entry_id and not m.mealie_note_id]:
            plan.meals.remove(meal)
        _fill(db, plan, [day_row], user)
    db.flush()
    build_shopping_list(db, plan, user)


def _fill(db: Session, plan: MealPlan, days: list[PlanDay], user: User | None) -> int:
    """Chooses dishes for the empty places on these days, from the same pools as a new plan."""
    household = db.get(Household, plan.household_id)
    ctx = build_context(db, household, max(plan.start_date, local_today()), user)
    recipes = visible_recipes(db, household.id)
    # Refused dishes count as used, so they are not suggested again straight away.
    used = {m.recipe_id for m in plan.meals if m.recipe_id}
    added = 0
    for day_row in sorted(days, key=lambda d: d.date):
        adults = ctx.settings.default_adults + day_row.guest_adults
        children = ctx.settings.default_children + day_row.guest_children
        people = portions(adults, children, ctx.settings.child_portion)
        for meal_type in plan.meal_types:
            if any(m.date == day_row.date and m.meal_type == meal_type and m.status != "rejected" for m in plan.meals):
                continue
            options = _choose(eligible_recipes(recipes, meal_type, ctx.profile), people, day_row.date, ctx, used, Counter(), frozenset())
            if not options or (meal_type == "dinner" and options[0].recipe.id in used):
                continue
            plan.meals.append(_meal_from(plan, day_row.date, meal_type, options[0], adults, children))
            used.add(options[0].recipe.id)
            added += 1
    return added


def fill_empty_slots(db: Session, plan: MealPlan, user: User | None) -> int:
    """Suggests dishes for the days still empty, such as around a dish someone chose by hand."""
    today = local_today()
    days = [d for d in plan.days if not d.skipped and d.date >= today]
    added = _fill(db, plan, days, user)
    db.flush()
    build_shopping_list(db, plan, user)
    return added


def _members_to_tell(db: Session, household_id: int) -> list[User]:
    members = db.scalars(select(User).where(User.household_id == household_id, User.is_active.is_(True)))
    return [m for m in members if user_settings(m.settings).features.plan_updates]


def _plan_ready(db: Session, plan: MealPlan, person: User) -> bool:
    shopping = db.scalar(select(ShoppingList).where(ShoppingList.plan_id == plan.id))
    data = {
        "plan_id": plan.id,
        "start": plan.start_date.isoformat(),
        "end": plan.end_date.isoformat(),
        "meals": len(plan.meals),
        "total": (shopping.summary or {}).get("total") if shopping else None,
    }
    return notify(db, person, "plan_ready", data, dedupe_key=f"plan_ready:{plan.id}") is not None


def notify_plan_ready(db: Session, plan: MealPlan, made_by: User | None = None) -> int:
    """A plan is only a suggestion until people have seen it. Whoever made it has."""
    others = [m for m in _members_to_tell(db, plan.household_id) if made_by is None or m.id != made_by.id]
    return sum(1 for member in others if _plan_ready(db, plan, member))


def notify_plan_shared(db: Session, plan: MealPlan, person: User) -> None:
    if person.is_active and user_settings(person.settings).features.plan_updates:
        _plan_ready(db, plan, person)


def refresh_open_plans(db: Session, today: date | None = None) -> int:
    """New offers change what a plan costs, so plans are priced again after a crawl.

    A list someone has begun ticking off is left alone, as they may be standing in the store
    with it. People hear about it when the difference matters.
    """
    today = today or local_today()
    lists = db.scalars(
        select(ShoppingList)
        .join(MealPlan, MealPlan.id == ShoppingList.plan_id)
        .where(MealPlan.end_date >= today, ShoppingList.completed_at.is_(None))
    ).all()
    refreshed = 0
    for shopping in lists:
        if any(item.checked for item in shopping.items):
            continue
        plan = db.get(MealPlan, shopping.plan_id)
        household = db.get(Household, plan.household_id)
        creator = db.get(User, plan.created_by) if plan.created_by else None
        before = (shopping.summary or {}).get("total")
        ctx = build_context(db, household, max(plan.start_date, today), creator)
        for meal in plan.meals:
            if meal.status == "rejected" or meal.recipe is None or meal.date < today:
                continue
            people = meal_portions(meal, ctx.settings.child_portion)
            cost, normal, on_offer, *_ = _cost_recipe(meal.recipe, people, ctx)
            meal.estimated_cost, meal.normal_cost = cost, normal
            reasons = [r for r in meal.reasons or [] if r.get("code") != "on_offer"]
            if on_offer and normal and normal > cost:
                reasons.insert(0, {"code": "on_offer", "items": on_offer[:4], "saving": round(normal - cost, 2)})
            meal.reasons = reasons
        after = (build_shopping_list(db, plan, creator, ctx.book).summary or {}).get("total")
        refreshed += 1
        if before and after is not None and abs(after - before) >= max(_NOTABLE_CHANGE_KR, before * _NOTABLE_CHANGE_SHARE):
            data = {"plan_id": plan.id, "old": before, "new": after}
            for member in _members_to_tell(db, plan.household_id):
                notify(db, member, "list_changed", data, dedupe_key=f"list_changed:{shopping.id}:{today.isoformat()}")
    return refreshed


def recipes_matching_ingredients(
    db: Session, household: Household, ingredients: list[str], meal_type: str | None = None, limit: int = 5
) -> list[dict]:
    """Which dishes can be made from these ingredients, and what is missing."""
    profile = household_profile(db, household)
    wanted = [key_for(i) for i in ingredients if i and i.strip()]
    results = []
    for recipe in visible_recipes(db, household.id):
        if meal_type and meal_type not in (recipe.meal_types or []):
            continue
        if allergy_conflict(recipe, profile.allergies) or not diet_allows(profile.diet_mode, recipe):
            continue
        lines = [line.get("name", "") for line in recipe.ingredients or []]
        needed = [line for line in lines if not is_staple(line)]
        if not needed:
            continue
        keys = {key_for(line): line for line in needed}
        have = [keys[k] for k in keys if k in wanted or any(text_matches(w, keys[k]) for w in wanted)]
        if not have:
            continue
        missing = [line for line in needed if line not in have]
        results.append(
            {
                "recipe_id": recipe.id,
                "title": recipe.title,
                "title_en": recipe.title_en,
                "meal_types": recipe.meal_types,
                "have": have,
                "missing": missing,
                "coverage": round(len(have) / len(needed), 2),
                "steps": recipe.steps,
                "mealie_slug": recipe.mealie_slug,
            }
        )
    results.sort(key=lambda r: (-len(r["have"]), len(r["missing"])))
    return results[:limit]
