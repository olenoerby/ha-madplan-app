"""The "New recipes" page: real recipes around current offers, and adding them to Mealie."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.ai.engine import AiUnavailable
from app.api.deps import CurrentUser, Db, household_of, require_feature
from app.integrations.mealie import MealieError
from app.models import Recipe, User
from app.services import new_recipes as service
from app.services.app_settings import get_ai_settings
from app.services.mealie_sync import ALLERGY as IMPORT_ALLERGY
from app.services.mealie_sync import MealieOff, RecipeRefused
from app.services.recipe_pages import PageRefused
from app.services.recipes import allergy_conflict, household_profile
from app.settings_schema import MealType, household_settings

router = APIRouter(tags=["new-recipes"])


def _own_web_recipe(db: Session, recipe_id: int, user: User) -> Recipe:
    recipe = db.get(Recipe, recipe_id)
    if recipe is None or recipe.household_id != user.household_id or recipe.source != "web":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    return recipe


def _card(db: Session, recipe: Recipe) -> dict:
    return {"id": recipe.id, "title": recipe.title, "link": service.recipe_link(recipe, *service.household_links(db, recipe.household_id))}


@router.get("/new-recipes")
def list_new_recipes(user: CurrentUser, db: Db, meal_type: MealType | None = None) -> dict:
    require_feature(user, "new_recipes")
    return service.new_recipes(db, household_of(db, user), user, meal_type)


class SearchIn(BaseModel):
    meal_types: list[MealType] | None = None


@router.post("/new-recipes/search")
def search(payload: SearchIn, user: CurrentUser, db: Db) -> dict:
    """Costs a little of the AI budget, so it only runs when someone asks, or on the schedule."""
    require_feature(user, "new_recipes")
    household = household_of(db, user)
    meal_types = list(payload.meal_types or household_settings(household.settings).default_meal_types)
    count = get_ai_settings(db).plan_ai_candidates or 6
    try:
        result = service.find_new_recipes(db, household, user, meal_types, count, locale=user.locale)
    except AiUnavailable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"ai_unavailable:{exc.reason}") from exc
    db.commit()
    return {"found": len(result.saved), "refused": sum(result.refused.values()), "refused_why": dict(result.refused), "no_offers": result.no_offers, "links": result.links}


class LinkIn(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    meal_type: MealType = "dinner"


@router.post("/new-recipes/from-link", status_code=201)
def from_link(payload: LinkIn, user: CurrentUser, db: Db) -> dict:
    """A page someone found themselves, which is how this works without AI."""
    require_feature(user, "new_recipes")
    try:
        recipe = service.add_from_link(db, household_of(db, user), payload.url, payload.meal_type)
    except PageRefused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"page_{exc.reason}") from exc
    db.commit()
    return _card(db, recipe)


@router.post("/new-recipes/{recipe_id}/to-mealie")
def to_mealie(recipe_id: int, user: CurrentUser, db: Db) -> dict:
    """Mealie imports the page now, as a person is waiting and it takes only seconds."""
    require_feature(user, "new_recipes")
    recipe = _own_web_recipe(db, recipe_id, user)
    household = household_of(db, user)
    # Someone may have added an allergy since the recipe was found.
    if allergy_conflict(recipe, household_profile(db, household).allergies):
        raise HTTPException(status.HTTP_409_CONFLICT, IMPORT_ALLERGY)
    try:
        linked = service.add_to_mealie(db, household, recipe)
    except MealieOff as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    except RecipeRefused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    except MealieError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "mealie_error") from exc
    db.commit()
    return _card(db, linked)


@router.post("/new-recipes/{recipe_id}/dismiss")
def dismiss(recipe_id: int, user: CurrentUser, db: Db) -> dict:
    require_feature(user, "new_recipes")
    service.dismiss(db, _own_web_recipe(db, recipe_id, user))
    db.commit()
    return {"ok": True}


@router.delete("/new-recipes/{recipe_id}/dismiss")
def undo_dismiss(recipe_id: int, user: CurrentUser, db: Db) -> dict:
    require_feature(user, "new_recipes")
    service.dismiss(db, _own_web_recipe(db, recipe_id, user), dismissed=False)
    db.commit()
    return {"ok": True}
