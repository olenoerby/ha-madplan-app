"""Keeps a meal plan in step with Mealie, when a person asks for it.

Nothing happens in Mealie because a meal was accepted. "Save and send to Mealie" on the plan page
queues what has changed, and the worker then brings Mealie in line with the plan: recipes are
imported from the page they came from, and the calendar, the shopping list and a note about guests
follow. A Mealie outage delays this without losing it, as the work is retried.

MadPlan never writes a recipe into Mealie from text of its own. A recipe either exists there
already, or Mealie imports it from a real page and the result is checked. A dish with neither only
goes on the calendar by name.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import ha
from app.core.db import local_today, utcnow
from app.integrations.mealie import MealieClient, MealieError, parse_mealie_ingredient
from app.models import Household, MealPlan, PlannedMeal, Recipe, User
from app.services.allergens import recipe_conflict
from app.services.app_settings import get_integrations, mealie_client, mealie_config, mealie_public_base, save_integrations
from app.services.notifications import notify
from app.services.pantry import load_stock
from app.services.pricing import is_staple, key_for
from app.services.recipes import household_profile, meal_portions, meal_type_words, meal_types_from_labels, same_dish
from app.services.text import normalize, text_matches, to_base_unit, words
from app.settings_schema import HouseholdSettings, household_settings, user_settings

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 6
_TITLE_FILLER = {"med", "og", "i", "på", "til", "af", "hjemmelavet", "bagt", "bagte", "stegt", "stegte", "fad", "ovn", "kartofler", "ris", "salat", "sovs"}

# Why a recipe did not make it into Mealie. The plan page and the recipe page say these to people.
NO_PAGE = "import_no_page"
UNREADABLE = "import_unreadable"
NO_IMAGE = "import_no_image"
ALLERGY = "import_allergy"


class RecipeRefused(MealieError):
    """Mealie works, but this recipe is not going in. Trying again would give the same answer."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MealieOff(Exception):
    """``code`` is mealie_not_connected or mealie_sync_off."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def find_in_mealie(client: MealieClient, title: str) -> dict | None:
    for summary in client.search_recipes(title):
        if same_dish(summary.get("name", ""), title):
            return summary
    return None


def household_copy(db: Session, recipe: Recipe, household_id: int) -> Recipe:
    """Built-in recipes are shared by all households, but a Mealie link belongs to just one."""
    if recipe.household_id == household_id:
        return recipe
    existing = db.scalar(
        select(Recipe).where(Recipe.household_id == household_id, Recipe.title == recipe.title)
    )
    if existing:
        return existing
    copy = Recipe(
        household_id=household_id,
        source=recipe.source,
        title=recipe.title,
        title_en=recipe.title_en,
        description=recipe.description,
        meal_types=list(recipe.meal_types or []),
        servings=recipe.servings,
        ingredients=list(recipe.ingredients or []),
        steps=list(recipe.steps or []),
        steps_en=list(recipe.steps_en) if recipe.steps_en else None,
        tags=list(recipe.tags or []),
        source_url=recipe.source_url,
    )
    db.add(copy)
    db.flush()
    return copy


def is_usable_import(remote: dict, wanted_title: str | None = None) -> bool:
    """Whether a scraped page gave the dish that was asked for.

    Mealie reports success even for a page it could not read, filling in placeholder text such
    as "Could not detect ingredients".
    """
    ingredients = remote.get("recipeIngredient") or []
    steps = remote.get("recipeInstructions") or []
    text = " ".join(str(line.get("note") or line.get("display") or "") for line in ingredients)
    text += " ".join(str(step.get("text") or "") for step in steps)
    if len(ingredients) < 2 or not steps or "could not detect" in text.lower():
        return False
    if wanted_title is None:
        return True
    name = remote.get("name") or ""
    significant = [w for w in words(wanted_title) if w not in _TITLE_FILLER]
    return not significant or any(text_matches(word, name) for word in significant)


def _has_image(remote: dict) -> bool:
    # Mealie leaves it empty when a page names no picture, and has been known to say so in words.
    return remote.get("image") not in (None, "", "no image")


def _allergy_in(remote: dict, allergies: list[str]) -> str | None:
    """Read from what Mealie holds, which can differ from what MadPlan read earlier."""
    texts: list[str] = [str(remote.get("description") or "")]
    for line in remote.get("recipeIngredient") or []:
        texts.append(parse_mealie_ingredient(line)["name"])
        texts.append(str(line.get("display") or line.get("note") or ""))
    texts += [str(step.get("text") or "") for step in remote.get("recipeInstructions") or []]
    return recipe_conflict(texts, str(remote.get("name") or ""), allergies)


def _import_page(client: MealieClient, recipe: Recipe, allergies: list[str]) -> dict:
    """Mealie reads the page itself. What it made of it is taken out again if it is not all there."""
    try:
        slug = client.create_from_url(recipe.source_url)
    except MealieError as exc:
        if exc.status_code in (400, 422):
            raise RecipeRefused(UNREADABLE) from exc
        raise
    remote = client.get_recipe(slug)
    problem = None
    if not is_usable_import(remote, recipe.title):
        problem = UNREADABLE
    elif not _has_image(remote):
        problem = NO_IMAGE
    elif _allergy_in(remote, allergies):
        problem = ALLERGY
    if problem:
        log.info("Mealie's import of %s was refused: %s", recipe.source_url, problem)
        client.delete_recipe(slug)
        raise RecipeRefused(problem)
    return remote


def label_for_meals(client: MealieClient, remote: dict, meal_types: list[str]) -> list[str]:
    """Puts the recipe in Mealie's categories and tags for its meals, such as "Aftensmad" or
    "Dinner", when Mealie has them. None are made: the household's own names are kept. Returns
    the names added. Failing here never undoes an import."""
    wanted = {normalize(word) for meal in meal_types or [] for word in meal_type_words(meal)}
    if not wanted or not remote.get("slug"):
        return []
    try:
        changes: dict[str, list] = {}
        added: list[str] = []
        for field, existing in (("recipeCategory", client.categories()), ("tags", client.tags())):
            current = list(remote.get(field) or [])
            have = {c.get("id") for c in current}
            matches = [o for o in existing if normalize(o.get("name") or "") in wanted and o.get("id") not in have]
            if matches:
                changes[field] = current + [{"id": o["id"], "name": o["name"], "slug": o.get("slug")} for o in matches]
                added += [o["name"] for o in matches]
        if changes:
            client.update_recipe(remote["slug"], changes)
        return added
    except MealieError as exc:
        log.warning("could not set categories on %s in Mealie: %s", remote.get("slug"), exc)
        return []


def ensure_recipe_in_mealie(db: Session, client: MealieClient, recipe: Recipe, household_id: int) -> Recipe:
    """The household's recipe with its Mealie id and slug filled in, or RecipeRefused."""
    if recipe.household_id == household_id and recipe.mealie_id and recipe.mealie_slug:
        return recipe
    allergies = household_profile(db, db.get(Household, household_id)).allergies

    found = find_in_mealie(client, recipe.title)
    # One of the household's own with the same name is fine, unless it holds what someone must
    # not eat. The recipe here passed that check, and that says nothing about the one in Mealie.
    if found and not _allergy_in(client.get_recipe(found["slug"]), allergies):
        linked = household_copy(db, recipe, household_id)
        linked.mealie_slug, linked.mealie_id = found.get("slug"), found.get("id")
        return linked

    if recipe.source != "web" or not recipe.source_url:
        raise RecipeRefused(NO_PAGE)
    remote = _import_page(client, recipe, allergies)
    recipe.mealie_slug, recipe.mealie_id = remote.get("slug"), remote.get("id")
    label_for_meals(client, remote, recipe.meal_types)
    return recipe


def _missing_ingredients_only(db: Session, remote: dict, household_id: int, scale: float = 1.0) -> list[dict] | None:
    """The recipe's ingredients without what is at home, or None when that is all of them.

    The same idea of "at home" as MadPlan's own list: the pantry, and staples like salt and oil
    if the household counts those as always there. A leftover smaller than what the dish needs
    does not count, and the line then stays as it is, as a Mealie line cannot be made smaller.
    """
    stock = load_stock(db, household_id)
    assume_staples = household_settings(db.get(Household, household_id).settings).assume_staples
    all_lines = remote.get("recipeIngredient") or []

    def at_home(line: dict) -> bool:
        parsed = parse_mealie_ingredient(line)
        name, quantity = parsed["name"], parsed.get("quantity")
        if assume_staples and is_staple(name):
            return True
        key = key_for(name)
        if not stock.has(key):
            return False
        needed, unit = to_base_unit(quantity * scale if quantity is not None else None, parsed.get("unit"))
        have = stock.amount(key, unit)
        return have is None or needed is None or have >= needed

    needed = [line for line in all_lines if not at_home(line)]
    return needed if len(needed) != len(all_lines) else None


def _people(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def guest_note(meal: PlannedMeal, settings: HouseholdSettings, locale: str = "da") -> tuple[str, str] | None:
    """A title and a text saying who is coming, when more are eating than the household's usual."""
    extra_adults = max(0, meal.adults - settings.default_adults)
    extra_children = max(0, meal.children - settings.default_children)
    if not extra_adults and not extra_children:
        return None
    if locale == "en":
        adults, children, label, total, word = ("adult", "adults"), ("child", "children"), "Guests", "In all", "and"
    else:
        adults, children, label, total, word = ("voksen", "voksne"), ("barn", "børn"), "Gæster", "I alt", "og"
    guests = [_people(n, *names) for n, names in ((extra_adults, adults), (extra_children, children)) if n]
    everyone = [_people(n, *names) for n, names in ((meal.adults, adults), (meal.children, children)) if n]
    return f"{label}: {f' {word} '.join(guests)}", f"{total} {f' {word} '.join(everyone)}: {meal.title}"


def _scale(meal: PlannedMeal, settings: HouseholdSettings) -> float | None:
    if meal.recipe is None:
        return None
    return round(meal_portions(meal, settings.child_portion) / max(meal.recipe.servings or 1, 0.5), 2)


def _ignore_gone(remove: Callable[[], object]) -> None:
    """Someone may have removed it in Mealie already, which is what was wanted anyway."""
    try:
        remove()
    except MealieError as exc:
        if exc.status_code != 404:
            raise


def _take_entry_out(meal: PlannedMeal, client: MealieClient, list_id: str | None, settings: HouseholdSettings) -> None:
    """Removes the calendar entry and what it put on the shopping list."""
    sent = meal.mealie_sent or {}
    if meal.mealie_entry_id:
        _ignore_gone(lambda: client.delete_plan_entry(meal.mealie_entry_id))
    # Plans sent by earlier versions kept no record, so those go by the meal as it is now.
    recipe_id = sent.get("mealie_recipe_id") if meal.mealie_sent else (meal.recipe.mealie_id if meal.recipe else None)
    scale = sent.get("scale") if meal.mealie_sent else _scale(meal, settings)
    if recipe_id and (sent.get("listed") or not meal.mealie_sent):
        target_list = list_id or client.ensure_shopping_list(ha.options().mealie_shopping_list)
        _ignore_gone(lambda: client.remove_recipe_from_list(target_list, recipe_id, scale or 1.0))
    meal.mealie_entry_id = None


def _recipe_for(db: Session, client: MealieClient, meal: PlannedMeal, household_id: int) -> tuple[Recipe, dict] | None:
    """The recipe as Mealie has it. None when the dish has no real recipe to link to."""
    if meal.recipe is None:
        return None
    for _ in range(2):
        try:
            recipe = ensure_recipe_in_mealie(db, client, meal.recipe, household_id)
        except RecipeRefused as exc:
            if exc.code == NO_PAGE:
                return None
            raise
        if recipe.id != meal.recipe_id:
            meal.recipe_id, meal.recipe = recipe.id, recipe
        try:
            return recipe, client.get_recipe(recipe.mealie_slug)
        except MealieError as exc:
            if exc.status_code != 404:
                raise
            # Deleted in Mealie since. It gets found again or imported anew, or the dish goes
            # on the calendar by name.
            recipe.mealie_slug = recipe.mealie_id = None
    return None


def sync_meal(db: Session, meal: PlannedMeal, client: MealieClient, list_id: str | None, user: User | None = None) -> None:
    """Brings Mealie in line with one accepted meal. Safe to run again: done steps are not repeated."""
    plan = meal.plan
    household = db.get(Household, plan.household_id)
    settings = household_settings(household.settings)
    allergies = household_profile(db, household).allergies
    sent = dict(meal.mealie_sent or {})

    def record(**changes: object) -> None:
        # Saved after each step: if the next one fails, a retry must not double-book anything.
        sent.update(changes)
        meal.mealie_sent = dict(sent)
        db.commit()

    # Before anything is taken out, so that a refused recipe leaves Mealie as it was.
    found = _recipe_for(db, client, meal, plan.household_id)
    if found and _allergy_in(found[1], allergies):
        raise RecipeRefused(ALLERGY)
    scale = _scale(meal, settings)
    # So that people looking at Mealie can tell what MadPlan put there from what a person did.
    mark = madplan_mark(user.locale if user else "da")

    moved = _moved(meal)
    if meal.mealie_entry_id and (sent.get("recipe_id") != meal.recipe_id or sent.get("scale") != scale):
        _take_entry_out(meal, client, list_id, settings)
        sent.pop("listed", None)
        record(recipe_id=None, scale=None, mealie_recipe_id=None)
    elif meal.mealie_entry_id and moved:
        # Only the day changed. Mealie's shopping list stays as it is, and the entry moves.
        _ignore_gone(lambda: client.delete_plan_entry(meal.mealie_entry_id))
        meal.mealie_entry_id = None
    if moved and meal.mealie_note_id:
        _ignore_gone(lambda: client.delete_plan_entry(meal.mealie_note_id))
        meal.mealie_note_id = None
        record(note=None)

    recipe = found[0] if found else None
    if not meal.mealie_entry_id:
        if recipe:
            entry = client.create_plan_entry(meal.date, meal.meal_type, title=meal.title, recipe_id=recipe.mealie_id, scale=scale or 1.0, text=mark)
        else:
            entry = client.create_plan_entry(meal.date, meal.meal_type, title=meal.title, text=mark)
        meal.mealie_entry_id = str(entry.get("id"))
        record(recipe_id=meal.recipe_id, scale=scale, mealie_recipe_id=recipe.mealie_id if recipe else None, date=meal.date.isoformat())

    if recipe and not sent.get("listed"):
        target_list = list_id or client.ensure_shopping_list(ha.options().mealie_shopping_list)
        client.add_recipe_to_list(target_list, recipe.mealie_id, scale or 1.0, _missing_ingredients_only(db, found[1], plan.household_id, scale or 1.0))
        record(listed=True)

    # An entry of its own, as Mealie's planner shows only the recipe on an entry with a recipe.
    note = guest_note(meal, settings, user.locale if user else "da")
    title = note[0] if note else None
    if sent.get("note") != title or (title and not meal.mealie_note_id):
        if meal.mealie_note_id:
            _ignore_gone(lambda: client.delete_plan_entry(meal.mealie_note_id))
            meal.mealie_note_id = None
        if note:
            created = client.create_plan_entry(meal.date, meal.meal_type, title=note[0], text=f"{note[1]}\n\n{mark}")
            meal.mealie_note_id = str(created.get("id"))
        record(note=title)

    meal.sync_status, meal.sync_error = "synced", None


MARKS = {"da": "Fra MadPlan", "en": "From MadPlan"}


def madplan_mark(locale: str) -> str:
    return MARKS.get(locale, MARKS["da"])


def is_marked(entry: dict) -> bool:
    text = entry.get("text") or ""
    return any(mark in text for mark in MARKS.values())


def _moved(meal: PlannedMeal) -> bool:
    """Whether the meal is on another day than the one Mealie has. Unknown for older records."""
    sent_day = (meal.mealie_sent or {}).get("date")
    return sent_day is not None and sent_day != meal.date.isoformat()


def unsync_meal(db: Session, meal: PlannedMeal, client: MealieClient, list_id: str | None) -> None:
    """Takes a meal that is no longer on the plan back out of Mealie."""
    settings = household_settings(db.get(Household, meal.plan.household_id).settings)
    _take_entry_out(meal, client, list_id, settings)
    if meal.mealie_note_id:
        _ignore_gone(lambda: client.delete_plan_entry(meal.mealie_note_id))
        meal.mealie_note_id = None
    meal.mealie_sent = None
    meal.sync_status, meal.sync_error = "none", None


def is_unsent(meal: PlannedMeal, settings: HouseholdSettings) -> bool:
    """Whether Mealie differs from what the plan says about this meal now."""
    if meal.date < local_today() or meal.sync_status == "pending":
        return False
    if meal.status != "accepted":
        return bool(meal.mealie_entry_id or meal.mealie_note_id)
    if not meal.mealie_entry_id:
        return True
    sent = meal.mealie_sent
    if sent is None:
        # Sent by an earlier version, which kept no record. Replacing the dish clears the status.
        return meal.sync_status == "none"
    note = guest_note(meal, settings)
    # The text of the note may be in another language, so only whether there is one is compared
    # here. A different number of guests changes the scale as well.
    return (
        sent.get("recipe_id") != meal.recipe_id
        or _moved(meal)
        or sent.get("scale") != _scale(meal, settings)
        or bool(sent.get("note")) != bool(note)
        or meal.sync_status == "failed"
    )


def plan_state(db: Session, plan: MealPlan) -> dict:
    """What the plan page says next to the send button."""
    config = mealie_config(db, plan.household_id)
    settings = household_settings(db.get(Household, plan.household_id).settings)
    today = local_today()
    return {
        "connected": config is not None,
        "sync_enabled": bool(config and config.sync_enabled),
        "to_send": sum(1 for m in plan.meals if is_unsent(m, settings)),
        "sending": sum(1 for m in plan.meals if m.sync_status == "pending"),
        "failed": sum(1 for m in plan.meals if m.sync_status == "failed"),
        "sent": sum(1 for m in plan.meals if m.status == "accepted" and m.mealie_entry_id),
        "undecided": sum(1 for m in plan.meals if m.status == "pending" and m.date >= today),
    }


def send_plan(db: Session, plan: MealPlan) -> int:
    """The "Save and send to Mealie" button. The one place that queues a plan's meals for the worker."""
    config = mealie_config(db, plan.household_id)
    if config is None:
        raise MealieOff("mealie_not_connected")
    if not config.sync_enabled:
        raise MealieOff("mealie_sync_off")
    settings = household_settings(db.get(Household, plan.household_id).settings)
    queued = 0
    for meal in plan.meals:
        if is_unsent(meal, settings):
            meal.sync_status, meal.sync_error, meal.sync_attempts = "pending", None, 0
            queued += 1
    db.flush()
    return queued


def unsent_accepted(db: Session, household_id: int, today=None) -> list[PlannedMeal]:
    """Accepted meals from today on that Mealie does not have as they are now."""
    today = today or local_today()
    settings = household_settings(db.get(Household, household_id).settings)
    meals = db.scalars(
        select(PlannedMeal)
        .join(MealPlan, MealPlan.id == PlannedMeal.plan_id)
        .where(MealPlan.household_id == household_id, PlannedMeal.date >= today, PlannedMeal.status == "accepted")
        .order_by(PlannedMeal.date)
    )
    return [m for m in meals if is_unsent(m, settings)]


def send_household(db: Session, household_id: int) -> int:
    """The "Save and send to Mealie" button for every current plan of the household, as from a phone.

    Someone still pressed something: this is the same button, only somewhere else."""
    today = local_today()
    plans = db.scalars(select(MealPlan).where(MealPlan.household_id == household_id, MealPlan.end_date >= today))
    return sum(send_plan(db, plan) for plan in plans)


def remind_unsent(db: Session, today=None) -> int:
    """The day before the first accepted meal that has not gone to Mealie, people are told.

    Nothing is sent: that waits for someone to press the button, here or in Home Assistant."""
    today = today or local_today()
    told = 0
    for household in db.scalars(select(Household)):
        config = mealie_config(db, household.id)
        if config is None or not config.sync_enabled:
            continue
        meals = unsent_accepted(db, household.id, today)
        if not meals or (meals[0].date - today).days > 1:
            continue
        first = meals[0]
        data = {"count": len(meals), "first": first.date.isoformat(), "title": first.title, "plan_id": first.plan_id}
        members = db.scalars(select(User).where(User.household_id == household.id, User.is_active.is_(True)))
        key = f"mealie_unsent:{household.id}:{first.date.isoformat()}"
        for member in members:
            if user_settings(member.settings).features.plan_updates and notify(db, member, "mealie_unsent", data, dedupe_key=key):
                told += 1
    db.flush()
    return told


def process_pending(db: Session) -> dict:
    """Worker job: handles the meals someone has asked to have sent."""
    counts = {"synced": 0, "failed": 0}
    meals = db.scalars(
        select(PlannedMeal).where(PlannedMeal.sync_status == "pending", PlannedMeal.sync_attempts < MAX_ATTEMPTS)
    ).all()
    for meal in meals:
        plan = meal.plan
        config = mealie_config(db, plan.household_id)
        client = mealie_client(db, plan.household_id)
        if config is None or client is None or not config.sync_enabled:
            continue
        user = db.get(User, plan.created_by) if plan.created_by else None
        try:
            with client:
                if meal.status == "accepted":
                    sync_meal(db, meal, client, config.shopping_list_id, user)
                else:
                    unsync_meal(db, meal, client, config.shopping_list_id)
            counts["synced"] += 1
        except RecipeRefused as exc:
            db.rollback()
            meal.sync_status, meal.sync_error, meal.sync_attempts = "failed", exc.code, MAX_ATTEMPTS
            counts["failed"] += 1
        except MealieError as exc:
            db.rollback()
            meal.sync_attempts += 1
            meal.sync_error = str(exc)[:500]
            meal.sync_status = "failed" if meal.sync_attempts >= MAX_ATTEMPTS else "pending"
            counts["failed"] += 1
        db.commit()
    return counts


def _meal_types_from(remote: dict) -> list[str]:
    labels = [t.get("name") for t in (remote.get("tags") or []) + (remote.get("recipeCategory") or []) if isinstance(t, dict)]
    return meal_types_from_labels(labels) or ["dinner"]


def pull_recipes(db: Session, household_id: int, client: MealieClient, max_recipes: int = 200) -> int:
    """Brings the household's Mealie recipes into the planner's pool. Only fetches new ones."""
    known = {
        r.mealie_slug: r
        for r in db.scalars(select(Recipe).where(Recipe.household_id == household_id, Recipe.mealie_slug.is_not(None)))
    }
    titles = {r.title for r in db.scalars(select(Recipe).where(Recipe.household_id == household_id))}
    added = 0
    page = 1
    listed: set[str] = set()
    seen = 0
    while seen < max_recipes:
        batch = client.list_recipes(page=page, per_page=50)
        items = batch.get("items", [])
        if not items:
            break
        for summary in items:
            seen += 1
            slug = summary.get("slug")
            if slug:
                listed.add(slug)
            if not slug or slug in known or summary.get("name") in titles:
                continue
            try:
                remote = client.get_recipe(slug)
                recipe = Recipe(
                    household_id=household_id,
                    source="mealie",
                    title=(remote.get("name") or slug)[:200],
                    description=remote.get("description"),
                    meal_types=_meal_types_from(remote),
                    servings=remote.get("recipeServings") or 4,
                    ingredients=[parse_mealie_ingredient(i) for i in remote.get("recipeIngredient") or []],
                    steps=[s.get("text", "") for s in remote.get("recipeInstructions") or [] if s.get("text")],
                    tags=[t.get("name", "") for t in remote.get("tags") or [] if isinstance(t, dict)],
                    source_url=remote.get("orgURL"),
                    mealie_slug=slug,
                    mealie_id=remote.get("id"),
                )
            except MealieError as exc:
                log.warning("could not read the recipe %s from Mealie: %s", slug, exc)
                continue
            except (TypeError, ValueError, AttributeError) as exc:
                # One odd recipe should not keep all the others out of the plans.
                log.warning("could not understand the recipe %s from Mealie: %s", slug, exc)
                continue
            db.add(recipe)
            added += 1
        db.commit()
        if page >= (batch.get("total_pages") or 1):
            break
        page += 1
    # Only when the whole list was read, and it held something. A recipe deleted in Mealie should
    # not be suggested as being there, or get a button leading nowhere.
    if listed and seen < max_recipes:
        for slug, recipe in known.items():
            if slug not in listed:
                recipe.mealie_slug = recipe.mealie_id = None
                if recipe.source == "web":
                    # Someone took it out on purpose, so it should not come back as a new idea.
                    recipe.dismissed_at = utcnow()
        db.commit()
    return added


def remember_group(db: Session, household_id: int, client: MealieClient) -> None:
    """A link to a recipe in Mealie has the group in its address, and it rarely changes."""
    group = client.current_user().get("groupSlug")
    stored = get_integrations(db, household_id)
    if group and stored.mealie_group_slug != group:
        save_integrations(db, household_id, stored.model_copy(update={"mealie_group_slug": group}))


def calendar_entries(db: Session, plans: list[MealPlan], household_id: int, start, end, ignore: set[str] | None = None) -> list[dict]:
    """Mealie's own calendar for the period, leaving out what these plans put there themselves."""
    client = mealie_client(db, household_id)
    if client is None:
        return []
    known = {str(i) for plan in plans for m in plan.meals for i in (m.mealie_entry_id, m.mealie_note_id) if i} | (ignore or set())
    try:
        with client:
            entries = client.list_plan_entries(start, end)
    except MealieError as exc:
        log.info("could not read the Mealie calendar: %s", exc)
        return []
    base = mealie_public_base(db, household_id)
    group = get_integrations(db, household_id).mealie_group_slug
    results = []
    for entry in entries:
        if str(entry.get("id")) in known:
            continue
        # Only the asked-for days, whatever the server sends.
        if not str(start) <= str(entry.get("date") or "")[:10] <= str(end):
            continue
        recipe = entry.get("recipe") or {}
        slug = recipe.get("slug")
        results.append(
            {
                "id": str(entry.get("id")),
                # A note has no recipe, and may say anything: "eating out" as well as a dish.
                "kind": "recipe" if entry.get("recipeId") or slug else "note",
                "date": entry.get("date"),
                "meal_type": entry.get("entryType"),
                "title": recipe.get("name") or entry.get("title") or "",
                "mealie_slug": slug,
                "url": mealie_recipe_url(base, group, slug),
                # Sent by MadPlan from a plan that has since been deleted, or by another installation.
                "from_madplan": is_marked(entry),
            }
        )
    return results


def mealie_planner_url(base: str | None) -> str | None:
    """Mealie's own meal planner, as people open it."""
    return f"{base}/household/mealplan/planner/view" if base else None


def mealie_recipe_url(base: str | None, group: str | None, slug: str | None) -> str | None:
    """With the group, as that address sends someone not logged in through the login and back."""
    if not base or not slug:
        return None
    return f"{base}/g/{group}/r/{slug}" if group else f"{base}/recipe/{slug}"
