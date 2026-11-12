"""Recipe lookups and what can be concluded from a recipe's ingredients."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Household, Recipe, User
from app.services.allergens import recipe_conflict
from app.services.builtin_recipes import BUILTIN_RECIPES
from app.services.builtin_recipes_en import STEPS_EN
from app.services.catalog import find_ingredient
from app.services.text import normalize, text_matches, words
from app.settings_schema import EffectiveProfile, effective_profile, household_settings

_SAME_DISH = 0.86
_MEAT_WORDS = (
    "kød", "okse", "svin", "gris", "kylling", "kalkun", "lam", "and", "bacon", "skinke", "pølse",
    "medister", "flæsk", "frikadelle", "kotelet", "mørbrad", "steg", "gullasch",
)
_FISH_WORDS = ("fisk", "laks", "torsk", "sej", "rødspætte", "tun", "makrel", "sild", "rejer", "reje", "ørred", "krabbe", "musling")
_ANIMAL_WORDS = ("mælk", "fløde", "smør", "ost", "yoghurt", "skyr", "æg", "creme fraiche", "honning", "mayonnaise")
_PLANT_MILK = ("kokosmælk", "havremælk", "sojamælk", "mandelmælk", "jordnøddesmør")

_MEAL_TYPE_WORDS = {
    "breakfast": ("morgenmad", "breakfast", "brunch"),
    "lunch": ("frokost", "lunch"),
    "dinner": ("aftensmad", "middag", "dinner", "hovedret", "main course", "hovedretter"),
    "snack": ("snack", "snacks", "mellemmåltid", "dessert", "kage", "kager"),
    "drink": ("drink", "drinks", "drik", "drikkevarer", "cocktail", "cocktails"),
}
# What goes with a meal, and is not one: rice with curry on its own would be a poor dinner.
_NOT_A_MEAL = ("tilbehør", "side dish", "sauce", "dressing", "marinade", "krydderi", "krydderiblanding", "dip")


@dataclass(frozen=True)
class Traits:
    proteins: frozenset[str]
    has_meat: bool
    has_fish: bool
    has_animal_product: bool
    carb_heavy_lines: int


def meal_types_from_labels(labels: list[str]) -> list[str]:
    """Which meals a recipe's own categories and tags point to, if any."""
    cleaned = [normalize(label) for label in labels if label]
    return [
        meal_type for meal_type, names in _MEAL_TYPE_WORDS.items()
        if any(any(text_matches(name, label) for name in names) for label in cleaned)
    ]


def not_a_meal(labels: list[str]) -> bool:
    """True when the categories say side dish or sauce, and nothing says that it is a meal too."""
    cleaned = [normalize(label) for label in labels if label]
    return not meal_types_from_labels(labels) and any(any(text_matches(word, label) for word in _NOT_A_MEAL) for label in cleaned)


def same_dish(a: str, b: str) -> bool:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio() >= _SAME_DISH


def household_profile(db: Session, household: Household) -> EffectiveProfile:
    members = db.scalars(
        select(User).where(User.household_id == household.id, User.is_active.is_(True))
    ).all()
    return effective_profile(household.settings, [m.settings for m in members])


def seed_builtin_recipes(db: Session) -> int:
    """Adds any built-in recipes that are missing, and English steps to those without."""
    existing = {r.title: r for r in db.scalars(select(Recipe).where(Recipe.source == "builtin"))}
    added = 0
    for recipe in BUILTIN_RECIPES:
        current = existing.get(recipe.title)
        if current is not None:
            if current.steps_en is None and recipe.title in STEPS_EN:
                current.steps_en = list(STEPS_EN[recipe.title])
            continue
        db.add(
            Recipe(
                household_id=None,
                source="builtin",
                title=recipe.title,
                title_en=recipe.title_en,
                meal_types=list(recipe.meal_types),
                servings=recipe.servings,
                ingredients=[
                    {"name": name, "quantity": quantity, "unit": unit}
                    for name, quantity, unit in recipe.ingredients
                ],
                steps=list(recipe.steps),
                steps_en=list(STEPS_EN.get(recipe.title, ())) or None,
                tags=list(recipe.tags),
            )
        )
        added += 1
    return added


def is_real_recipe(recipe: Recipe) -> bool:
    """A recipe someone can go and read: in Mealie, or on the web page it was read from."""
    return bool(recipe.mealie_slug) or (recipe.source == "web" and bool(recipe.source_url))


def visible_recipes(db: Session, household_id: int) -> list[Recipe]:
    """What may be suggested: real recipes, unless someone has said no thanks.

    MadPlan's own dishes only if the household asked for them, and what an earlier version had
    a model write never, unless it went to Mealie and someone has kept it there.
    """
    household = db.get(Household, household_id)
    use_builtin = household is not None and household_settings(household.settings).builtin_dishes
    recipes = [
        r
        for r in db.scalars(select(Recipe).where(or_(Recipe.household_id.is_(None), Recipe.household_id == household_id)))
        if r.dismissed_at is None and (is_real_recipe(r) or (use_builtin and r.source == "builtin"))
    ]
    # A household's copy hides the built-in one.
    own = {r.title for r in recipes if r.household_id == household_id}
    return [r for r in recipes if r.household_id == household_id or r.title not in own]


def ingredient_names(recipe: Recipe) -> list[str]:
    return [line.get("name", "") for line in recipe.ingredients or []]


def recipe_traits(recipe: Recipe) -> Traits:
    proteins: set[str] = set()
    has_meat = has_fish = has_animal = False
    carb_heavy = 0
    for name in ingredient_names(recipe):
        ingredient = find_ingredient(name)
        normalized = normalize(name)
        if ingredient is not None:
            if ingredient.protein:
                proteins.add(ingredient.protein)
            if ingredient.protein in ("beef", "pork", "chicken", "lamb"):
                has_meat = True
            if ingredient.protein == "fish":
                has_fish = True
            if ingredient.carb_heavy:
                carb_heavy += 1
        text_words = words(name)
        if any(any(term in word for word in text_words) for term in _MEAT_WORDS if len(term) > 3) or any(
            term in text_words for term in _MEAT_WORDS if len(term) <= 3
        ):
            has_meat = True
        if any(any(term in word for word in text_words) for term in _FISH_WORDS if len(term) > 3) or any(
            term in text_words for term in _FISH_WORDS if len(term) <= 3
        ):
            has_fish = True
        if not any(plant in normalized for plant in _PLANT_MILK):
            for term in _ANIMAL_WORDS:
                if (len(term) <= 3 and term in text_words) or (len(term) > 3 and term in normalized):
                    has_animal = True
    return Traits(
        proteins=frozenset(proteins),
        has_meat=has_meat,
        has_fish=has_fish,
        has_animal_product=has_animal or has_meat or has_fish,
        carb_heavy_lines=carb_heavy,
    )


def diet_allows(mode: str | None, recipe: Recipe, traits: Traits | None = None) -> bool:
    """The diets that rule food out. Mediterranean only changes ranking."""
    if not mode:
        return True
    traits = traits or recipe_traits(recipe)
    if mode == "vegetarian":
        return not traits.has_meat and not traits.has_fish
    if mode == "vegan":
        return not traits.has_animal_product
    if mode == "pescetarian":
        return not traits.has_meat
    if mode == "keto":
        return traits.carb_heavy_lines == 0
    if mode == "low_carb":
        return traits.carb_heavy_lines <= 1
    return True


def diet_bonus(mode: str | None, recipe: Recipe, traits: Traits | None = None) -> float:
    traits = traits or recipe_traits(recipe)
    if mode == "mediterranean":
        score = 0.0
        if traits.has_fish or "legumes" in traits.proteins:
            score += 10
        if traits.proteins & {"beef", "pork", "lamb"}:
            score -= 8
        if "middelhav" in (recipe.tags or []):
            score += 8
        return score
    if mode and mode in (recipe.tags or []):
        return 5.0
    return 0.0


def allergy_conflict(recipe: Recipe, allergies: list[str]) -> str | None:
    """Reads everything the recipe says. Failing towards excluding: "drys med nødder" in a step
    counts, and so does what the parsed name of an ingredient left out."""
    if not allergies:
        return None
    texts = ingredient_names(recipe) + [line.get("text", "") for line in recipe.ingredients or []]
    texts += [*(recipe.steps or []), recipe.description or ""]
    return recipe_conflict(texts, recipe.title, allergies)


def eligible_recipes(recipes: list[Recipe], meal_type: str, profile: EffectiveProfile) -> list[Recipe]:
    """Applies the hard rules: right meal, nothing anyone is allergic to, allowed by the diet."""
    return [
        r for r in recipes
        if meal_type in (r.meal_types or [])
        and not allergy_conflict(r, profile.allergies)
        and diet_allows(profile.diet_mode, r)
    ]


def term_hits_recipe(term: str, recipe: Recipe) -> bool:
    """For likes and dislikes. Shares the allergy tables, so "grisekød" also means bacon and
    chops, and matches inside compounds, so "kylling" also means "kyllingelår"."""
    return recipe_conflict(ingredient_names(recipe), recipe.title, [term]) is not None


def portions(adults: int, children: int, child_portion: float) -> float:
    return max(adults + children * child_portion, 0.5)


def meal_portions(meal, child_portion: float) -> float:
    """What is cooked for a meal: who eats it, and the portions saved for a later meal."""
    return portions(meal.adults, meal.children, child_portion) + (meal.leftover_portions or 0)
