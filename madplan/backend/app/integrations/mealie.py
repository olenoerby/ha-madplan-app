"""Client for the Mealie REST API, checked against the OpenAPI document of Mealie v3.

There is deliberately no way here to create a recipe from text: Mealie imports a recipe from its
page or it does not get one, so nothing a model wrote can end up there as a recipe.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from app.services.text import split_ingredient

ENTRY_TYPES = {"breakfast", "lunch", "dinner", "side", "snack", "drink", "dessert"}


class MealieError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MealieClient:
    def __init__(self, base_url: str, token: str, transport: httpx.BaseTransport | None = None, timeout: float = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MealieClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise MealieError(f"could not reach Mealie: {exc}") from exc
        if response.is_error:
            raise MealieError(f"{method} {path} returned {response.status_code}: {response.text[:300]}", response.status_code)
        return response

    def about(self) -> dict[str, Any]:
        return self._request("GET", "/api/app/about").json()

    def current_user(self) -> dict[str, Any]:
        return self._request("GET", "/api/users/self").json()

    # Recipes

    def search_recipes(self, query: str, per_page: int = 10) -> list[dict[str, Any]]:
        params = {"search": query, "perPage": per_page, "page": 1}
        return self._request("GET", "/api/recipes", params=params).json().get("items", [])

    def list_recipes(self, page: int = 1, per_page: int = 50) -> dict[str, Any]:
        params = {"perPage": per_page, "page": page, "orderBy": "updatedAt", "orderDirection": "desc"}
        return self._request("GET", "/api/recipes", params=params).json()

    def get_recipe(self, slug: str) -> dict[str, Any]:
        return self._request("GET", f"/api/recipes/{slug}").json()

    def create_from_url(self, url: str, include_tags: bool = True) -> str:
        """Lets Mealie scrape a recipe page. Returns the new recipe's slug."""
        payload = {"url": url, "includeTags": include_tags, "includeCategories": False}
        return self._request("POST", "/api/recipes/create/url", json=payload, timeout=90).json()

    def delete_recipe(self, slug: str) -> None:
        self._request("DELETE", f"/api/recipes/{slug}")

    # Meal plan

    def list_plan_entries(self, start: date, end: date) -> list[dict[str, Any]]:
        params = {"start_date": start.isoformat(), "end_date": end.isoformat(), "perPage": 200, "page": 1}
        return self._request("GET", "/api/households/mealplans", params=params).json().get("items", [])

    def create_plan_entry(
        self,
        day: date,
        entry_type: str,
        title: str = "",
        recipe_id: str | None = None,
        text: str = "",
        scale: float = 1.0,
    ) -> dict[str, Any]:
        if entry_type not in ENTRY_TYPES:
            entry_type = "side"
        payload = {
            "date": day.isoformat(),
            "entryType": entry_type,
            "title": title,
            "text": text,
            "recipeId": recipe_id,
            "recipeScale": scale,
        }
        try:
            return self._request("POST", "/api/households/mealplans", json=payload).json()
        except MealieError as exc:
            # Older servers only know breakfast, lunch, dinner and side.
            if exc.status_code == 422 and entry_type in ("snack", "drink", "dessert"):
                payload["entryType"] = "side"
                return self._request("POST", "/api/households/mealplans", json=payload).json()
            raise

    def delete_plan_entry(self, entry_id: str) -> None:
        self._request("DELETE", f"/api/households/mealplans/{entry_id}")

    # Shopping lists

    def shopping_lists(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/households/shopping/lists", params={"perPage": 100, "page": 1}).json().get("items", [])

    def create_shopping_list(self, name: str) -> dict[str, Any]:
        return self._request("POST", "/api/households/shopping/lists", json={"name": name}).json()

    def ensure_shopping_list(self, name: str = "MadPlan") -> str:
        for shopping_list in self.shopping_lists():
            if shopping_list.get("name") == name:
                return shopping_list["id"]
        return self.create_shopping_list(name)["id"]

    def get_shopping_list(self, list_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/households/shopping/lists/{list_id}").json()

    def add_recipe_to_list(
        self,
        list_id: str,
        recipe_id: str,
        scale: float = 1.0,
        ingredients: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Adds a recipe's ingredients. Passing ``ingredients`` adds only those."""
        item: dict[str, Any] = {"recipeId": recipe_id, "recipeIncrementQuantity": scale}
        if ingredients is not None:
            item["recipeIngredients"] = ingredients
        return self._request("POST", f"/api/households/shopping/lists/{list_id}/recipe", json=[item]).json()

    def remove_recipe_from_list(self, list_id: str, recipe_id: str, scale: float = 1.0) -> dict[str, Any]:
        payload = {"recipeDecrementQuantity": scale}
        return self._request("POST", f"/api/households/shopping/lists/{list_id}/recipe/{recipe_id}/delete", json=payload).json()


def parse_mealie_ingredient(raw: dict[str, Any]) -> dict[str, Any]:
    """Reads a Mealie ingredient back into MadPlan's simple shape."""
    food = (raw.get("food") or {}).get("name") if isinstance(raw.get("food"), dict) else None
    unit = (raw.get("unit") or {}).get("name") if isinstance(raw.get("unit"), dict) else None
    note = (raw.get("note") or "").strip()
    quantity = raw.get("quantity") or None
    if food:
        return {"name": food, "quantity": quantity, "unit": unit}
    text = note or (raw.get("display") or "").strip()
    # Recipes MadPlan created itself keep the amount apart and start the note with the unit.
    if quantity and not unit and " " in text:
        first, rest = text.split(" ", 1)
        if first.lower() in {"g", "kg", "ml", "dl", "l", "stk", "tsk", "spsk", "fed"}:
            return {"name": rest, "quantity": quantity, "unit": first.lower()}
    if quantity or unit:
        return {"name": text, "quantity": quantity, "unit": unit}
    # A scraped recipe: everything is plain text, as in "400 g hakket oksekød".
    name, parsed_quantity, parsed_unit = split_ingredient(text)
    return {"name": name, "quantity": parsed_quantity, "unit": parsed_unit}
