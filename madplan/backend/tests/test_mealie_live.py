"""Against a real Mealie server. Mocks can only confirm our own idea of how Mealie behaves.

Skipped unless these are set, as CI does and as docs/development.md describes:
    MADPLAN_TEST_MEALIE_URL    such as http://localhost:19925
    MADPLAN_TEST_MEALIE_TOKEN  an API token from that server
    MADPLAN_TEST_RECIPE_PAGE   optional: the address of a real recipe page to import
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.integrations.mealie import MealieClient, MealieError, parse_mealie_ingredient
from app.models import Recipe
from app.services.mealie_sync import RecipeRefused, ensure_recipe_in_mealie, process_pending, pull_recipes, unsync_meal
from app.services.recipes import seed_builtin_recipes

from .conftest import household_of_admin

URL = os.environ.get("MADPLAN_TEST_MEALIE_URL")
TOKEN = os.environ.get("MADPLAN_TEST_MEALIE_TOKEN")
# A real recipe page, for the one test that goes out on the internet.
REAL_PAGE = os.environ.get("MADPLAN_TEST_RECIPE_PAGE")

pytestmark = pytest.mark.skipif(not (URL and TOKEN), reason="no Mealie server configured")

# Far enough ahead that these tests never collide with anything else on the server.
TEST_DAY = date(2031, 3, 3)


def write_recipe(client: MealieClient, name, description, servings, ingredients, steps, source_url=None) -> dict:
    """Puts a recipe straight into Mealie, as the app itself has no way to. For setting up tests."""
    slug = client._request("POST", "/api/recipes", json={"name": name}).json()
    patch = {
        "description": description or "",
        "recipeServings": servings,
        "recipeIngredient": [_ingredient(line) for line in ingredients],
        "recipeInstructions": [{"text": step} for step in steps],
    }
    if source_url:
        patch["orgURL"] = source_url
    client._request("PATCH", f"/api/recipes/{slug}", json=patch)
    return client.get_recipe(slug)


def _ingredient(line: dict) -> dict:
    quantity = line.get("quantity")
    note = f"{line.get('unit') or ''} {line.get('name', '')}".strip()
    text = f"{quantity:g} {note}" if quantity else note
    return {"quantity": float(quantity) if quantity else 0, "unit": None, "food": None, "note": note, "display": text, "originalText": text}


@pytest.fixture
def mealie():
    with MealieClient(URL, TOKEN) as client:
        yield client


@pytest.fixture
def unique():
    return f"MadPlan test {uuid.uuid4().hex[:8]}"


def test_connection(mealie):
    assert mealie.about()["version"]
    assert mealie.current_user()["id"]


def test_bad_token_gives_a_clear_error():
    with MealieClient(URL, "not-a-token") as client, pytest.raises(MealieError) as exc:
        client.current_user()
    assert exc.value.status_code == 401


def test_full_recipe_keeps_everything_we_send(mealie, unique):
    """Description, ingredient list and steps, as the specification lists."""
    recipe = write_recipe(
        mealie,
        unique,
        "En god beskrivelse",
        4,
        [{"name": "hakket oksekød", "quantity": 500, "unit": "g"}, {"name": "løg", "quantity": 1, "unit": "stk"}, {"name": "salt", "quantity": None, "unit": None}],
        ["Brun kødet.", "Tilsæt løg."],
        "https://example.com/opskrift",
    )
    try:
        assert recipe["name"] == unique
        assert recipe["description"] == "En god beskrivelse"
        assert recipe["recipeServings"] == 4
        assert recipe["orgURL"] == "https://example.com/opskrift"
        assert [s["text"] for s in recipe["recipeInstructions"]] == ["Brun kødet.", "Tilsæt løg."]
        lines = recipe["recipeIngredient"]
        assert len(lines) == 3
        assert lines[0]["quantity"] == 500
        assert "hakket oksekød" in lines[0]["note"]
    finally:
        mealie.delete_recipe(recipe["slug"])


def test_ingredients_read_back_into_our_shape(mealie, unique):
    recipe = write_recipe(mealie, unique, "", 4, [{"name": "hakket oksekød", "quantity": 500, "unit": "g"}], ["x"])
    try:
        parsed = parse_mealie_ingredient(recipe["recipeIngredient"][0])
        assert parsed == {"name": "hakket oksekød", "quantity": 500, "unit": "g"}
    finally:
        mealie.delete_recipe(recipe["slug"])


def test_search_finds_a_recipe_by_name(mealie, unique):
    recipe = write_recipe(mealie, unique, "", 4, [], ["x"])
    try:
        assert any(r["slug"] == recipe["slug"] for r in mealie.search_recipes(unique))
    finally:
        mealie.delete_recipe(recipe["slug"])


@pytest.mark.parametrize("entry_type", ["breakfast", "lunch", "dinner", "snack", "drink"])
def test_all_meal_types_go_on_the_calendar(mealie, unique, entry_type):
    """Morgenmad, Frokost, Snacks, Drinks and Aftensmad, as the specification lists."""
    recipe = write_recipe(mealie, unique, "", 4, [], ["x"])
    try:
        entry = mealie.create_plan_entry(TEST_DAY, entry_type, title=unique, recipe_id=recipe["id"], scale=1.5)
        assert entry["entryType"] == entry_type
        assert entry["recipeId"] == recipe["id"]
        found = mealie.list_plan_entries(TEST_DAY, TEST_DAY)
        assert any(e["id"] == entry["id"] for e in found)
        mealie.delete_plan_entry(entry["id"])
        assert not any(e["id"] == entry["id"] for e in mealie.list_plan_entries(TEST_DAY, TEST_DAY))
    finally:
        mealie.delete_recipe(recipe["slug"])


def test_shopping_list_gets_the_recipe_scaled(mealie, unique):
    recipe = write_recipe(mealie, unique, "", 4, [{"name": "hakket oksekød", "quantity": 500, "unit": "g"}], ["x"])
    list_id = mealie.create_shopping_list(unique)["id"]
    try:
        mealie.add_recipe_to_list(list_id, recipe["id"], scale=1.5)
        items = mealie.get_shopping_list(list_id)["listItems"]
        assert len(items) == 1
        assert items[0]["quantity"] == 750
        assert "hakket oksekød" in items[0]["note"]

        mealie.remove_recipe_from_list(list_id, recipe["id"], scale=1.5)
        assert mealie.get_shopping_list(list_id)["listItems"] == []
    finally:
        mealie.delete_recipe(recipe["slug"])


def test_shopping_list_can_get_only_some_ingredients(mealie, unique):
    recipe = write_recipe(
        mealie,
        unique, "", 4, [{"name": "hakket oksekød", "quantity": 500, "unit": "g"}, {"name": "løg", "quantity": 1, "unit": "stk"}], ["x"]
    )
    list_id = mealie.create_shopping_list(unique)["id"]
    try:
        only_meat = [line for line in recipe["recipeIngredient"] if "oksekød" in line["note"]]
        mealie.add_recipe_to_list(list_id, recipe["id"], 1, only_meat)
        items = mealie.get_shopping_list(list_id)["listItems"]
        assert len(items) == 1 and "oksekød" in items[0]["note"]
    finally:
        mealie.delete_recipe(recipe["slug"])


def test_ensure_shopping_list_reuses_the_same_list(mealie, unique):
    first = mealie.ensure_shopping_list(unique)
    assert mealie.ensure_shopping_list(unique) == first


# The whole flow, through MadPlan


@pytest.fixture
def configured(db):
    """Points the household at the real Mealie, with only real recipes to plan from."""
    from .conftest import set_options

    household = household_of_admin(db)
    set_options(mealie_url=URL, mealie_token=TOKEN)
    household.settings = {**(household.settings or {}), "builtin_dishes": False}
    db.commit()
    return household


def recipe_page(title: str, image: str = "https://example.invalid/billede.jpg") -> str:
    node = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": title,
        "image": image,
        "recipeYield": "4",
        "recipeIngredient": ["500 g hakket oksekød", "2 stk løg", "1 tsk salt"],
        "recipeInstructions": [{"@type": "HowToStep", "text": "Brun kødet."}, {"@type": "HowToStep", "text": "Tilsæt løg, og lad det simre."}],
    }
    return f'<html><head><script type="application/ld+json">{json.dumps(node)}</script></head><body></body></html>'


@pytest.fixture
def pages(monkeypatch):
    """Mealie's real parser, on a page handed to it, as it cannot reach a test site from its
    container. The picture is the one thing faked: pages asking for a dead link is covered below,
    and test_a_real_page_goes_all_the_way fetches a real one."""

    def create_from_url(self, url, include_tags=True):
        title = url.rsplit("/", 1)[-1].replace("-", " ")
        slug = self._request("POST", "/api/recipes/create/html-or-json", json={"data": recipe_page(title), "includeTags": False, "url": url}).json()
        self._request("PATCH", f"/api/recipes/{slug}", json={"image": "live", "orgURL": url})
        return slug

    monkeypatch.setattr(MealieClient, "create_from_url", create_from_url)


def web_recipe(db, household, title: str) -> Recipe:
    recipe = Recipe(
        household_id=household.id, source="web", title=title, servings=4, meal_types=["dinner"], steps=["Brun kødet.", "Tilsæt løg."], tags=[],
        ingredients=[{"name": "hakket oksekød", "quantity": 500, "unit": "g"}, {"name": "løg", "quantity": 2, "unit": "stk"}, {"name": "salt", "quantity": 1, "unit": "tsk"}],
        source_url=f"https://madsiden.example/{title.replace(' ', '-')}",
        image_url="https://madsiden.example/billede.jpg",
    )
    db.add(recipe)
    db.commit()
    return recipe


def plan_for(client, day: date) -> dict:
    response = client.post("/api/v1/plans", json={"mode": "range", "start": day.isoformat(), "end": day.isoformat(), "use_ai": False, "seed": 7})
    assert response.status_code == 201, response.text
    return response.json()


def test_nothing_reaches_mealie_before_the_button_and_then_all_of_it(admin_client, db, configured, mealie, pages, unique):
    """The main flow: a yes stays here, the button sends recipe, calendar, list and guests."""
    title = unique.lower()
    web_recipe(db, configured, title)
    plan = plan_for(admin_client, TEST_DAY)
    meal = plan["meals"][0]
    assert meal["link"]["label"] == "madsiden.example"
    admin_client.patch(f"/api/v1/plans/{plan['id']}/days/{TEST_DAY.isoformat()}", json={"guest_adults": 2, "guest_children": 1})
    result = admin_client.post(f"/api/v1/meals/{meal['id']}/vote", json={"vote": "yes"}).json()
    assert result["meals"][0]["sync_status"] == "none"

    db.expire_all()
    assert process_pending(db) == {"synced": 0, "failed": 0}
    assert mealie.list_plan_entries(TEST_DAY, TEST_DAY) == []
    assert not [r for r in mealie.search_recipes(unique) if r["name"] == title]

    assert admin_client.post(f"/api/v1/plans/{plan['id']}/send-to-mealie").json()["meals"][0]["sync_status"] == "pending"
    db.expire_all()
    assert process_pending(db) == {"synced": 1, "failed": 0}
    db.expire_all()

    synced = admin_client.get(f"/api/v1/plans/{plan['id']}").json()["meals"][0]
    assert synced["sync_status"] == "synced"
    assert synced["link"]["kind"] == "mealie"

    remote = mealie.get_recipe(synced["mealie_slug"])
    assert remote["name"] == title
    assert len(remote["recipeInstructions"]) == 2
    assert len(remote["recipeIngredient"]) == 3

    entries = mealie.list_plan_entries(TEST_DAY, TEST_DAY)
    entry = next(e for e in entries if e["recipeId"] == remote["id"])
    assert entry["entryType"] == "dinner"
    # Marked, so that people in Mealie can see it came from MadPlan, and from whom.
    from app.services.mealie_sync import is_marked

    assert is_marked(entry) and "MadPlan" in entry["text"]
    # Four adults and a child at 0.6, for a recipe for four.
    assert entry["recipeScale"] == pytest.approx(1.15)
    note = next(e for e in entries if not e["recipeId"])
    assert note["title"] == "Gæster: 2 voksne og 1 barn"
    assert title in note["text"]

    list_id = mealie.ensure_shopping_list("MadPlan")
    assert any(item["recipeReferences"] for item in mealie.get_shopping_list(list_id)["listItems"])
    # The page does not report what the plan put there itself.
    assert admin_client.get(f"/api/v1/plans/{plan['id']}/mealie-entries").json() == []

    # Saying no waits for the button too, and then takes all of it back out.
    admin_client.post(f"/api/v1/meals/{meal['id']}/vote", json={"vote": "no"})
    db.expire_all()
    process_pending(db)
    assert len(mealie.list_plan_entries(TEST_DAY, TEST_DAY)) == 2
    admin_client.post(f"/api/v1/plans/{plan['id']}/send-to-mealie")
    db.expire_all()
    assert process_pending(db) == {"synced": 1, "failed": 0}
    assert mealie.list_plan_entries(TEST_DAY, TEST_DAY) == []
    mealie.delete_recipe(remote["slug"])


def test_what_is_planned_in_mealie_is_reported_with_a_link_that_works(admin_client, db, configured, mealie, unique):
    day = date(2031, 3, 7)
    remote = write_recipe(mealie, unique, "", 4, [{"name": "laks", "quantity": 400, "unit": "g"}], ["Bag laksen."])
    entry = mealie.create_plan_entry(day, "dinner", title="", recipe_id=remote["id"])
    note = mealie.create_plan_entry(day, "lunch", title="Rester")
    try:
        web_recipe(db, configured, "noget andet")
        plan = plan_for(admin_client, day)
        assert admin_client.post("/api/v1/admin/setup/test").json()["mealie"]["ok"] is True
        found = admin_client.get(f"/api/v1/plans/{plan['id']}/mealie-entries").json()
        assert {(e["title"], e["meal_type"]) for e in found} == {(unique, "dinner"), ("Rester", "lunch")}
        dish = next(e for e in found if e["title"] == unique)
        assert dish["url"] == f"{URL}/g/{mealie.current_user()['groupSlug']}/r/{remote['slug']}"
    finally:
        mealie.delete_plan_entry(entry["id"])
        mealie.delete_plan_entry(note["id"])
        mealie.delete_recipe(remote["slug"])


def test_recipe_already_in_mealie_is_not_duplicated(db, configured, mealie, unique):
    existing = write_recipe(mealie, unique, "Fra Mealie", 4, [], ["x"])
    try:
        recipe = Recipe(household_id=configured.id, source="web", title=unique, servings=4, meal_types=["dinner"], ingredients=[], steps=["y"], source_url="https://example.com/x")
        db.add(recipe)
        db.flush()
        linked = ensure_recipe_in_mealie(db, mealie, recipe, configured.id)
        assert linked.mealie_slug == existing["slug"]
        assert len([r for r in mealie.search_recipes(unique) if r["name"] == unique]) == 1
    finally:
        mealie.delete_recipe(existing["slug"])


def test_a_dish_without_a_page_is_never_written_as_a_recipe(db, configured, mealie, unique):
    recipe = Recipe(household_id=configured.id, source="builtin", title=unique, servings=4, meal_types=["dinner"], ingredients=[{"name": "laks"}], steps=["Bag."])
    db.add(recipe)
    db.flush()
    with pytest.raises(RecipeRefused) as exc:
        ensure_recipe_in_mealie(db, mealie, recipe, configured.id)
    assert exc.value.code == "import_no_page"
    assert mealie.search_recipes(unique) == []


def test_pantry_items_stay_off_the_mealie_list(admin_client, db, configured, mealie, unique):
    from app.models import MealPlan, PantryItem

    list_id = mealie.create_shopping_list(f"MadPlan {uuid.uuid4().hex[:6]}")["id"]
    from app.services.app_settings import get_integrations, save_integrations

    stored = get_integrations(db, configured.id)
    stored.mealie_shopping_list_id = list_id
    save_integrations(db, configured.id, stored)
    remote = write_recipe(mealie, unique, "", 4, [{"name": "hakket oksekød", "quantity": 500, "unit": "g"}, {"name": "løg", "quantity": 2, "unit": "stk"}], ["Steg."])
    db.add(Recipe(household_id=configured.id, source="mealie", title=unique, servings=4, meal_types=["dinner"], steps=["Steg."], tags=[], mealie_slug=remote["slug"], mealie_id=remote["id"], ingredients=[{"name": "hakket oksekød", "quantity": 500, "unit": "g"}, {"name": "løg", "quantity": 2, "unit": "stk"}]))
    db.add(PantryItem(household_id=configured.id, name="løg"))
    db.commit()

    day = date(2031, 3, 4)
    plan = plan_for(admin_client, day)
    meal = plan["meals"][0]
    admin_client.post(f"/api/v1/meals/{meal['id']}/vote", json={"vote": "yes"})
    admin_client.post(f"/api/v1/plans/{plan['id']}/send-to-mealie")
    db.expire_all()
    assert process_pending(db) == {"synced": 1, "failed": 0}

    notes = " ".join(i["note"] or "" for i in mealie.get_shopping_list(list_id)["listItems"])
    assert "løg" not in notes
    assert "oksekød" in notes

    db.expire_all()
    synced = db.get(MealPlan, plan["id"]).meals[0]
    unsync_meal(db, synced, mealie, list_id)
    db.commit()
    mealie.delete_recipe(remote["slug"])


def test_a_leftover_does_not_keep_a_whole_ingredient_off_the_list(db, configured, mealie, unique):
    """Whole packs come home now, so a small rest of something is an everyday thing."""
    from app.models import PantryItem
    from app.services.mealie_sync import _missing_ingredients_only

    remote = write_recipe(mealie, unique, "", 4, [{"name": "hakket oksekød", "quantity": 500, "unit": "g"}, {"name": "løg", "quantity": 2, "unit": "stk"}], ["Steg."], None)
    try:
        db.add(PantryItem(household_id=configured.id, name="hakket oksekød", quantity=100, unit="g"))
        db.add(PantryItem(household_id=configured.id, name="løg", quantity=10, unit="stk"))
        db.commit()
        needed = _missing_ingredients_only(db, remote, configured.id)
        assert needed is not None
        notes = " ".join(str(line) for line in needed)
        assert "oksekød" in notes
        assert "løg" not in notes
    finally:
        mealie.delete_recipe(remote["slug"])


def test_staples_stay_off_the_mealie_list(db, configured, mealie, unique):
    """Salt and oil are at home, here as on MadPlan's own list."""
    from app.services.mealie_sync import _missing_ingredients_only

    remote = write_recipe(mealie, unique, "", 4, [{"name": "laks", "quantity": 400, "unit": "g"}, {"name": "olie", "quantity": 1, "unit": "spsk"}, {"name": "salt", "quantity": None, "unit": None}], ["x"])
    try:
        lines = _missing_ingredients_only(db, remote, configured.id)
        assert [line["note"] for line in lines] == ["g laks"]
    finally:
        mealie.delete_recipe(remote["slug"])


def test_a_retry_does_not_double_book_the_calendar(admin_client, db, configured, mealie, pages, monkeypatch, unique):
    """Mealie fails after the calendar step. The retry must pick up where it stopped."""
    web_recipe(db, configured, unique.lower())
    day = date(2031, 3, 5)
    plan = plan_for(admin_client, day)
    meal = plan["meals"][0]
    admin_client.post(f"/api/v1/meals/{meal['id']}/vote", json={"vote": "yes"})
    admin_client.post(f"/api/v1/plans/{plan['id']}/send-to-mealie")

    original = MealieClient.add_recipe_to_list
    calls = {"n": 0}

    def fail_once(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise MealieError("temporary failure", 503)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MealieClient, "add_recipe_to_list", fail_once)
    db.expire_all()
    assert process_pending(db) == {"synced": 0, "failed": 1}
    db.expire_all()
    assert process_pending(db) == {"synced": 1, "failed": 0}

    db.expire_all()
    from app.models import MealPlan

    synced = db.get(MealPlan, plan["id"]).meals[0]
    entries = [e for e in mealie.list_plan_entries(day, day) if e["recipeId"] == synced.recipe.mealie_id]
    assert len(entries) == 1
    slug = synced.recipe.mealie_slug
    unsync_meal(db, synced, mealie, None)
    db.commit()
    mealie.delete_recipe(slug)


def test_mealie_recipes_join_the_planners_pool(db, configured, mealie, unique):
    remote = write_recipe(mealie, unique, "", 4, [{"name": "laks", "quantity": 400, "unit": "g"}], ["Bag laksen."])
    try:
        assert pull_recipes(db, configured.id, mealie) >= 1
        recipe = db.scalar(select(Recipe).where(Recipe.title == unique))
        assert recipe.source == "mealie" and recipe.household_id == configured.id
        assert recipe.ingredients[0]["name"] == "laks"
        assert recipe.steps == ["Bag laksen."]
        # Nothing new the second time, so a pull stays cheap.
        assert pull_recipes(db, configured.id, mealie) == 0
    finally:
        mealie.delete_recipe(remote["slug"])


def test_entries_made_in_mealie_show_on_our_calendar(admin_client, configured, mealie, unique):
    day = date(2031, 3, 6)
    entry = mealie.create_plan_entry(day, "lunch", title=unique)
    try:
        data = admin_client.get("/api/v1/calendar", params={"start": day.isoformat(), "end": (day + timedelta(days=1)).isoformat()}).json()
        assert any(e["title"] == unique and e["meal_type"] == "lunch" for e in data["mealie"])
    finally:
        mealie.delete_plan_entry(entry["id"])


def test_connection_test_on_the_admin_page(admin_client, configured):
    result = admin_client.post("/api/v1/admin/setup/test").json()
    assert result["mealie"]["ok"] is True
    # Home Assistant is always there for the app, through the Supervisor.
    assert result["home_assistant"]["ok"] is True


def test_the_token_never_reaches_the_browser(admin_client, db, configured):
    """It is in the app's configuration in Home Assistant, and MadPlan neither shows nor keeps it."""
    setup = admin_client.get("/api/v1/admin/setup").json()
    assert setup["mealie"]["token_set"] is True
    assert TOKEN not in str(setup)

    from app.models import AppSetting

    db.expire_all()
    assert all(TOKEN not in str(row.value) for row in db.query(AppSetting).all())


def test_a_page_mealie_cannot_read_is_not_kept(db, configured, mealie, unique, monkeypatch):
    """Mealie answers with success and an empty recipe. That must not end up in the household's Mealie."""
    empty = write_recipe(mealie, unique, "", 4, [{"name": "Could not detect ingredients", "quantity": None, "unit": None}], ["Could not detect instructions"])
    monkeypatch.setattr(mealie, "create_from_url", lambda url: empty["slug"])
    recipe = Recipe(household_id=configured.id, source="web", title=f"{unique} rigtig", servings=4, meal_types=["dinner"], ingredients=[{"name": "laks", "quantity": 400, "unit": "g"}], steps=["Bag laksen."], source_url="https://example.com/x")
    db.add(recipe)
    db.flush()
    with pytest.raises(RecipeRefused) as exc:
        ensure_recipe_in_mealie(db, mealie, recipe, configured.id)
    assert exc.value.code == "import_unreadable"
    assert all(r["slug"] != empty["slug"] for r in mealie.search_recipes(unique))
    assert recipe.mealie_slug is None


def test_a_picture_mealie_could_not_fetch_means_the_recipe_is_taken_out(db, configured, mealie, unique, monkeypatch):
    """The page names a picture that is not there. Seen on the real server: the image stays empty."""
    title = unique.lower()

    def create_from_url(url):
        return mealie._request("POST", "/api/recipes/create/html-or-json", json={"data": recipe_page(title), "includeTags": False, "url": url}).json()

    monkeypatch.setattr(mealie, "create_from_url", create_from_url)
    recipe = web_recipe(db, configured, title)
    with pytest.raises(RecipeRefused) as exc:
        ensure_recipe_in_mealie(db, mealie, recipe, configured.id)
    assert exc.value.code == "import_no_image"
    assert not [r for r in mealie.search_recipes(unique) if r["name"] == title]


@pytest.mark.skipif(not REAL_PAGE, reason="only on request, as it depends on a site out there")
def test_a_real_page_goes_all_the_way(admin_client, db, configured, mealie):
    """MadPlan reads the page, Mealie imports it with its picture, and the button changes."""
    from app.services.recipe_pages import RecipeReader

    with RecipeReader() as reader:
        page = reader.read(REAL_PAGE)
    found = admin_client.post("/api/v1/new-recipes/from-link", json={"url": REAL_PAGE}).json()
    assert found["title"] == page.title
    assert found["link"]["kind"] == "web"
    added = admin_client.post(f"/api/v1/new-recipes/{found['id']}/to-mealie")
    assert added.status_code == 200, added.text
    assert added.json()["link"]["kind"] == "mealie"
    db.expire_all()
    slug = db.get(Recipe, found["id"]).mealie_slug
    remote = mealie.get_recipe(slug)
    try:
        assert remote["image"] not in (None, "", "no image")
        assert remote["orgURL"] == page.url or remote["orgURL"] == REAL_PAGE
        assert len(remote["recipeIngredient"]) == len(page.ingredients)
    finally:
        mealie.delete_recipe(slug)


def test_seeding_again_changes_nothing(db):
    assert seed_builtin_recipes(db) == 0


def test_a_recipe_gets_the_households_own_categories_and_tags_in_mealie(mealie, unique):
    """The changes MadPlan sends are in the shape the real server takes."""
    from app.services.mealie_sync import label_for_meals

    name = f"Aftensmad {uuid.uuid4().hex[:6]}"
    category = mealie._request("POST", "/api/organizers/categories", json={"name": name}).json()
    tag = mealie._request("POST", "/api/organizers/tags", json={"name": "Dinner"}).json() if not any(t["name"] == "Dinner" for t in mealie.tags()) else next(t for t in mealie.tags() if t["name"] == "Dinner")
    remote = write_recipe(mealie, unique, "", 4, [{"name": "laks", "quantity": 400, "unit": "g"}], ["Bag laksen."])
    try:
        # A category named like that is a household's own name for dinner only when it is exactly one.
        import app.services.mealie_sync as sync

        words = sync.meal_type_words
        sync.meal_type_words = lambda meal: (*words(meal), name.lower())
        try:
            added = label_for_meals(mealie, remote, ["dinner"])
        finally:
            sync.meal_type_words = words
        assert set(added) == {name, "Dinner"}
        after = mealie.get_recipe(remote["slug"])
        assert name in [c["name"] for c in after.get("recipeCategory") or []]
        assert "Dinner" in [t["name"] for t in after.get("tags") or []]
        # The recipe is otherwise as it was.
        assert after["name"] == unique and [s["text"] for s in after["recipeInstructions"]] == ["Bag laksen."]
    finally:
        mealie.delete_recipe(remote["slug"])
        mealie._request("DELETE", f"/api/organizers/categories/{category['id']}")
        if tag.get("id"):
            mealie._request("DELETE", f"/api/organizers/tags/{tag['id']}")
