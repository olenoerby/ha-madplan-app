"""Recipes the household does not have yet, from real pages, around what is on offer now.

A search asks a model for addresses and nothing else. Every page is then read by MadPlan itself,
and has to hold a whole recipe, so what people see is what the site published. Found recipes are
kept, as they can fit next week's offers too without paying for another search.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.engine import AiUnavailable, ensure_allowed
from app.ai.features import search_recipe_pages
from app.core.db import local_today, utcnow
from app.integrations.mealie import MealieError
from app.models import Household, Recipe, User
from app.services.app_settings import get_integrations, mealie_client, mealie_config, mealie_public_base
from app.services.catalog import INGREDIENTS, find_ingredient
from app.services.mealie_sync import MealieOff, ensure_recipe_in_mealie, mealie_recipe_url, remember_group
from app.services.planner import PlanContext, build_context, score_recipe
from app.services.pricing import is_staple
from app.services.recipe_pages import PageRefused, RecipePage, RecipeReader, domain_of
from app.services.recipes import (
    allergy_conflict,
    diet_allows,
    eligible_recipes,
    household_profile,
    portions,
    same_dish,
    visible_recipes,
)
from app.services.text import normalize

log = logging.getLogger(__name__)

ALREADY_KNOWN = "already_known"
ALLERGY = "allergy"
DIET = "diet"

# Well within Cloudflare's limit of 100 seconds for an answer, when a person is waiting.
_READING_SECONDS = 45
_MAX_OFFER_LINES = 20
_MAX_KNOWN_TITLES = 80
_MAX_ON_CARD = 5


@dataclass
class SearchResult:
    saved: list[Recipe] = field(default_factory=list)
    refused: Counter = field(default_factory=Counter)
    no_offers: bool = False
    # How many addresses the AI agent gave. None at all usually means it could not search the web.
    links: int = 0


def _same_address(url: str | None) -> str:
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower().removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}"


def offer_lines(ctx: PlanContext) -> list[str]:
    """The foods on offer now, as ingredients to search by. Meat and fish first, as dishes are
    built around those. Offer titles alone would not do, as a leaflet is also full of televisions."""
    found = []
    for ingredient in INGREDIENTS:
        offers = [] if ingredient.staple else ctx.book.current_offers(ingredient)
        if offers:
            offer = min(offers, key=lambda o: o.price)
            found.append((ingredient.protein is None, offer.price - (offer.pre_price or offer.price), ingredient.key, offer))
    found.sort(key=lambda row: row[:3])
    return [f"- {key}: {offer.title}, {offer.price:g} kr, {offer.store_name}" for _, _, key, offer in found[:_MAX_OFFER_LINES]]


def save_page(db: Session, household: Household, page: RecipePage) -> Recipe:
    """Keeps a page's recipe for this household, exactly as read. Raises PageRefused."""
    profile = household_profile(db, household)
    recipe = Recipe(
        household_id=household.id,
        source="web",
        title=page.title,
        description=page.description,
        meal_types=page.meal_types,
        servings=page.servings,
        ingredients=page.ingredients,
        steps=page.steps,
        source_url=page.url,
        image_url=page.image_url,
        tags=[],
    )
    if allergy_conflict(recipe, profile.allergies):
        raise PageRefused(ALLERGY, page.url)
    if not diet_allows(profile.diet_mode, recipe):
        raise PageRefused(DIET, page.url)
    db.add(recipe)
    db.flush()
    return recipe


def _reader() -> RecipeReader:
    """A function of its own, so tests can read from pages they made themselves."""
    return RecipeReader()


def find_new_recipes(
    db: Session,
    household: Household,
    user: User | None,
    meal_types: list[str],
    count: int,
    day: date | None = None,
    locale: str = "da",
    wish: str | None = None,
) -> SearchResult:
    """Searches for pages, reads them, and keeps those that hold a whole recipe.

    ``wish`` is what a person asked for in their own words, such as "something with chicken".

    Raises AiUnavailable when no search can be made. Without offers there is nothing to search
    around, so no money is spent.
    """
    day = day or local_today()
    ctx = build_context(db, household, day, user)
    lines = offer_lines(ctx)
    # Offers are what an open search goes by. A person saying what they want is enough on its own.
    if not lines and not wish:
        return SearchResult(no_offers=True)

    known = list(db.scalars(select(Recipe).where(Recipe.household_id == household.id)))
    titles = [r.title for r in known if r.dismissed_at is None][:_MAX_KNOWN_TITLES]
    # A few more than wanted, as many pages turn out not to hold a whole recipe.
    found = search_recipe_pages(household, user, meal_types, count * 2, lines, ctx.profile, titles, locale, wish=wish)

    result = SearchResult(links=len(found))
    addresses = {_same_address(r.source_url) for r in known if r.source_url}
    deadline = time.monotonic() + _READING_SECONDS
    with _reader() as reader:
        for link in found:
            if len(result.saved) >= count or time.monotonic() > deadline:
                break
            address = _same_address(link.url)
            if address in addresses:
                result.refused[ALREADY_KNOWN] += 1
                continue
            addresses.add(address)
            fallback = link.meal_type if link.meal_type in meal_types else meal_types[0]
            try:
                page = reader.read(link.url, fallback)
                if any(same_dish(page.title, r.title) for r in known) or _same_address(page.url) in addresses - {address}:
                    raise PageRefused(ALREADY_KNOWN, page.url)
                saved = save_page(db, household, page)
                result.saved.append(saved)
                # The same dish from another site is not a new one, within a search either.
                known.append(saved)
            except PageRefused as exc:
                result.refused[exc.reason] += 1
    log.info(
        "recipe search for household %d: the AI agent gave %d addresses, kept %d, refused %s",
        household.id,
        len(found),
        len(result.saved),
        dict(result.refused),
    )
    if not found:
        log.warning("the AI agent gave no addresses: it is probably not allowed to search the web")
    return result


# Versions of one dish that a search for it offers to choose between.
DISH_CHOICES = 3
_ANY_MEAL = ["dinner", "lunch", "breakfast", "snack", "drink"]
NOT_THE_DISH = "not_the_dish"


def _is_the_dish(asked: str, title: str) -> bool:
    """Whether a page's title is the dish that was asked for. A lasagne search once gave a lentil
    soup, so a word of what was asked must be in the title: "suppe" is in "Tomatsuppe"."""
    words = [w for w in normalize(asked).split() if len(w) >= 4] or normalize(asked).split()
    name = normalize(title)
    return any(word in name for word in words)


def find_dish_online(
    db: Session,
    household: Household,
    user: User | None,
    dish: str,
    count: int = DISH_CHOICES,
    locale: str = "da",
) -> tuple[list[Recipe], SearchResult]:
    """A few real recipes for a dish someone named, to choose between.

    Each page is read and checked as any other found page, and kept, so it can be put on a day
    or added to Mealie. A page found before is offered again rather than refused. Raises
    AiUnavailable when no search can be made.
    """
    ctx = build_context(db, household, local_today(), user)
    known = list(db.scalars(select(Recipe).where(Recipe.household_id == household.id)))
    by_address = {_same_address(r.source_url): r for r in known if r.source_url}
    found = search_recipe_pages(household, user, _ANY_MEAL, count * 2, [], ctx.profile, [], locale, dish=dish)

    result = SearchResult(links=len(found))
    chosen: list[Recipe] = []
    seen: set[str] = set()
    deadline = time.monotonic() + _READING_SECONDS
    with _reader() as reader:
        for link in found:
            if len(chosen) >= count or time.monotonic() > deadline:
                break
            address = _same_address(link.url)
            if address in seen:
                continue
            seen.add(address)
            if address in by_address:
                # Found before: still one of the choices, unless someone said no thanks to it.
                again = by_address[address]
                if again.dismissed_at is None and _is_the_dish(dish, again.title):
                    chosen.append(again)
                continue
            fallback = link.meal_type if link.meal_type in _ANY_MEAL else "dinner"
            try:
                page = reader.read(link.url, fallback)
                if not _is_the_dish(dish, page.title):
                    raise PageRefused(NOT_THE_DISH, page.url)
                if _same_address(page.url) in by_address:
                    chosen.append(by_address[_same_address(page.url)])
                    continue
                saved = save_page(db, household, page)
                by_address[_same_address(saved.source_url)] = saved
                chosen.append(saved)
                result.saved.append(saved)
            except PageRefused as exc:
                result.refused[exc.reason] += 1
    log.info(
        "dish search for household %d: the AI agent gave %d addresses, offered %d, refused %s",
        household.id,
        len(found),
        len(chosen),
        dict(result.refused),
    )
    return chosen, result


def add_from_link(db: Session, household: Household, url: str, meal_type: str = "dinner") -> Recipe:
    """A page someone found themselves. It has to hold a whole recipe like any other."""
    with _reader() as reader:
        page = reader.read(url, meal_type)
    existing = next(
        (r for r in db.scalars(select(Recipe).where(Recipe.household_id == household.id)) if r.source_url and _same_address(r.source_url) == _same_address(page.url)),
        None,
    )
    if existing:
        # Asking for it again counts for more than having said no to it once.
        existing.dismissed_at = None
        db.flush()
        return existing
    return save_page(db, household, page)


def _offers_in(recipe: Recipe, ctx: PlanContext) -> list[dict]:
    """The ingredients that are in the leaflets now, each with the leaflet's own price."""
    rows: dict[int, dict] = {}
    for line in recipe.ingredients or []:
        name = line.get("name", "")
        # Salt on offer is no reason to make a dish.
        if is_staple(name):
            continue
        ingredient = find_ingredient(name)
        offers = ctx.book.current_offers(ingredient) if ingredient else [o for o in ctx.book.matches(name, None) if o.kind == "offer"]
        if not offers:
            continue
        offer = min(offers, key=lambda o: o.price)
        rows.setdefault(
            offer.id,
            {
                "ingredient": name,
                "offer": offer.title,
                "store": offer.store_name,
                "price": offer.price,
                # Only what the leaflet says itself, as anything else would be an estimate.
                "before": offer.pre_price,
                "membership": offer.membership_required,
            },
        )
    return list(rows.values())


def recipe_link(recipe: Recipe, base: str | None, group: str | None) -> dict | None:
    """Where a person can read a recipe: in Mealie if it is there, or else on the page it came from."""
    url = mealie_recipe_url(base, group, recipe.mealie_slug)
    if url:
        return {"kind": "mealie", "url": url, "label": "Mealie"}
    if recipe.source_url and recipe.source_url.lower().startswith(("http://", "https://")):
        return {"kind": "web", "url": recipe.source_url, "label": domain_of(recipe.source_url) or recipe.source_url}
    return None


def household_links(db: Session, household_id: int) -> tuple[str | None, str | None]:
    return mealie_public_base(db, household_id), get_integrations(db, household_id).mealie_group_slug


def recipe_card(recipe: Recipe, ctx: PlanContext, people: float, base: str | None, group: str | None, today: date) -> dict:
    """A found recipe as the pages show it: what it costs, and what in it is on offer."""
    offers = _offers_in(recipe, ctx)
    scored = score_recipe(recipe, people, today, ctx, Counter(), frozenset())
    return {
        "id": recipe.id,
        "title": recipe.title,
        "description": recipe.description,
        "image_url": recipe.image_url,
        "meal_types": recipe.meal_types,
        "servings": recipe.servings,
        "link": recipe_link(recipe, base, group),
        "in_mealie": bool(recipe.mealie_slug),
        "cost": scored.cost,
        "people": people,
        "offers": offers[:_MAX_ON_CARD],
        "more_offers": max(0, len(offers) - _MAX_ON_CARD),
        "disliked": [r["term"] for r in scored.reasons if r["code"] == "disliked"],
        "score": scored.score,
    }


def cards_for(db: Session, household: Household, user: User, recipes: list[Recipe]) -> list[dict]:
    """Cards for these recipes, in the order given."""
    today = local_today()
    ctx = build_context(db, household, today, user)
    people = portions(ctx.settings.default_adults, ctx.settings.default_children, ctx.settings.child_portion)
    base, group = household_links(db, household.id)
    return [recipe_card(r, ctx, people, base, group, today) for r in recipes]


def new_recipes(db: Session, household: Household, user: User, meal_type: str | None = None) -> dict:
    """Found recipes that are not in Mealie yet and use something on offer now, best first."""
    today = local_today()
    year, week, _ = today.isocalendar()
    # The same order all week, rather than a page that rearranges itself on each visit.
    ctx = build_context(db, household, today, user, seed=year * 100 + week + household.id)
    people = portions(ctx.settings.default_adults, ctx.settings.default_children, ctx.settings.child_portion)
    pool = [r for r in visible_recipes(db, household.id) if r.source == "web" and not r.mealie_slug]
    allowed = {r.id: r for meal in ("breakfast", "lunch", "dinner", "snack", "drink") for r in eligible_recipes(pool, meal, ctx.profile)}
    base, group = household_links(db, household.id)

    cards = []
    others = []
    for recipe in allowed.values():
        if meal_type and meal_type not in (recipe.meal_types or []):
            continue
        card = recipe_card(recipe, ctx, people, base, group, today)
        # Something found may be worth keeping even when nothing in it is on offer this week.
        (cards if card["offers"] else others).append(card)
    cards.sort(key=lambda card: -card["score"])
    others.sort(key=lambda card: -card["score"])

    config = mealie_config(db, household.id)
    search_blocked = None
    try:
        ensure_allowed(db, "recipe_finder", household.id)
    except AiUnavailable as exc:
        search_blocked = exc.reason
    return {
        "recipes": cards,
        # Found recipes with nothing on offer now. They can still be planned, or added to Mealie.
        "others": others,
        "waiting_for_offers": len(others),
        "offers_known": bool(ctx.book.offers),
        "search_blocked": search_blocked,
        "mealie_connected": config is not None,
        "mealie_sync_enabled": bool(config and config.sync_enabled),
    }


def add_to_mealie(db: Session, household: Household, recipe: Recipe) -> Recipe:
    """Imports the page into Mealie now. Raises MealieOff, RecipeRefused or MealieError."""
    config = mealie_config(db, household.id)
    client = mealie_client(db, household.id)
    if config is None or client is None:
        raise MealieOff("mealie_not_connected")
    if not config.sync_enabled:
        raise MealieOff("mealie_sync_off")
    with client:
        linked = ensure_recipe_in_mealie(db, client, recipe, household.id)
        try:
            remember_group(db, household.id, client)
        except MealieError as exc:
            # The recipe is in. The link just takes the longer way round until next time.
            log.info("could not read the group from Mealie: %s", exc)
    return linked


def dismiss(db: Session, recipe: Recipe, dismissed: bool = True) -> None:
    """"No thanks": it leaves this page and the planner's pool, and is not searched for again."""
    recipe.dismissed_at = utcnow() if dismissed else None
    db.flush()
