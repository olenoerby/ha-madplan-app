"""Recipes come from real pages, and nothing goes to Mealie until someone presses the button.

The owner's rules for this were explicit: recipes must not be made up, they must come from a site
with all the information and a picture, the site is named on a button, and the planner adds
nothing to Mealie before "Gem og Send til Mealie". Guests are noted in Mealie's planner, and what
is already planned there is pointed out. Mealie's behaviour here is simulated from what the real
server does, and test_mealie_live.py runs the same flow against that.
"""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.ai.features import FoundPage
from app.core.db import local_today
from app.crawlers import policy
from app.models import Household, MealPlan, PlannedMeal, Recipe, User
from app.services import app_settings, new_recipes, recipe_pages
from app.services.app_settings import get_integrations, save_integrations
from app.services.mealie_sync import is_usable_import, process_pending, pull_recipes
from app.services.recipe_pages import PageRefused, RecipeReader, domain_of, parse_recipe_page
from app.settings_schema import HouseholdSettings
from app.worker import job_auto_plan, job_recipe_search, run_due_jobs

from .conftest import add_offer, household_of_admin, login_as, make_user, set_options
from .test_ai import fake_openai, text_message  # noqa: F401
from .test_planning import set_household

URL = "https://madsiden.dk/opskrift/kylling-i-karry"


def page(
    name="Kylling i karry",
    image="https://madsiden.dk/billeder/karry.jpg",
    ingredients=("600 g kyllingebryst (i tern)", "1 løg", "4 dl ris"),
    steps=({"@type": "HowToStep", "text": "Steg kyllingen gylden i en stor gryde."}, {"@type": "HowToStep", "text": "Tilsæt løg og karry, og lad det simre."}),
    servings="4 personer",
    category="Aftensmad",
    extra=None,
) -> str:
    node = {"@context": "https://schema.org", "@type": "Recipe", **(extra or {})}
    for key, value in (("name", name), ("image", image), ("recipeIngredient", list(ingredients) if ingredients else None), ("recipeInstructions", steps), ("recipeYield", servings), ("recipeCategory", category)):
        if value:
            node[key] = value
    return f'<html><head><script type="application/ld+json">{json.dumps(node)}</script></head><body>x</body></html>'


class FakeWeb:
    """The internet, as far as the reader is concerned. Pages are served, and requests noted."""

    def __init__(self) -> None:
        self.pages: dict[str, httpx.Response] = {}
        self.requests: list[str] = []

    def serve(self, url: str, markup: str = "") -> None:
        self.pages[url] = httpx.Response(200, text=markup or page(), headers={"content-type": "text/html; charset=utf-8"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url in self.pages:
            return self.pages[url]
        return httpx.Response(404, text="nope")


@pytest.fixture
def web(monkeypatch):
    fake = FakeWeb()
    monkeypatch.setattr(recipe_pages, "is_public_http_url", lambda url: url.startswith("https://") and "intern" not in url)
    monkeypatch.setattr(new_recipes, "_reader", lambda: RecipeReader(transport=httpx.MockTransport(fake.handler), sleep=lambda _: None))
    return fake


def read(fake: FakeWeb, url: str = URL):
    with RecipeReader(transport=httpx.MockTransport(fake.handler), sleep=lambda _: None) as reader:
        return reader.read(url)


def good_remote(title="Kylling i karry", image="aBcD", lines=("600 g kyllingebryst", "1 løg"), steps=("Steg kyllingen.",), url=URL) -> dict:
    slug = title.lower().replace(" ", "-")
    return {
        "id": f"id-{slug}",
        "slug": slug,
        "name": title,
        "image": image,
        "orgURL": url,
        "recipeServings": 4,
        "description": "",
        "recipeIngredient": [{"note": line, "display": line, "quantity": 0} for line in lines],
        "recipeInstructions": [{"text": step} for step in steps],
    }


class FakeMealie:
    """Answers as the real server was seen to, including success for a page it could not read."""

    def __init__(self) -> None:
        self.recipes: dict[str, dict] = {}
        self.imports: dict[str, dict] = {}
        self.entries: dict[int, dict] = {}
        self.listed: list[tuple[str, str, float]] = []
        self.calls: list[str] = []
        self.deleted: list[str] = []
        self.next_entry = 100
        self.fail_list = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append(f"{method} {path}")
        body = json.loads(request.content) if request.content else {}
        if path == "/api/users/self":
            return httpx.Response(200, json={"groupSlug": "home", "id": "u"})
        if path == "/api/recipes" and method == "POST":
            raise AssertionError("MadPlan tried to write a recipe from text")
        if path == "/api/recipes":
            search = (request.url.params.get("search") or "").lower()
            return httpx.Response(200, json={"items": [r for r in self.recipes.values() if search in r["name"].lower()], "total_pages": 1})
        if path == "/api/recipes/create/url":
            if body["url"] not in self.imports:
                return httpx.Response(400, json={"detail": "BAD_RECIPE_DATA"})
            remote = self.imports[body["url"]]
            self.recipes[remote["slug"]] = remote
            return httpx.Response(201, json=remote["slug"])
        if path.startswith("/api/recipes/"):
            slug = path.rsplit("/", 1)[-1]
            if method == "DELETE":
                self.deleted.append(slug)
                self.recipes.pop(slug, None)
                return httpx.Response(200, json={})
            return httpx.Response(200, json=self.recipes[slug]) if slug in self.recipes else httpx.Response(404, json={})
        if path == "/api/households/mealplans" and method == "POST":
            self.next_entry += 1
            entry = {"id": self.next_entry, **body, "recipe": next(({"slug": r["slug"], "name": r["name"]} for r in self.recipes.values() if r["id"] == body.get("recipeId")), None)}
            self.entries[self.next_entry] = entry
            return httpx.Response(201, json=entry)
        if path == "/api/households/mealplans":
            return httpx.Response(200, json={"items": list(self.entries.values())})
        if path.startswith("/api/households/mealplans/"):
            entry_id = int(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={}) if self.entries.pop(entry_id, None) else httpx.Response(404, json={})
        if path == "/api/households/shopping/lists":
            return httpx.Response(200, json={"items": [{"id": "L1", "name": "MadPlan"}]})
        if path.endswith("/delete"):
            self.listed.append(("remove", path.split("/")[-2], body["recipeDecrementQuantity"]))
            return httpx.Response(200, json={})
        if path.endswith("/recipe"):
            if self.fail_list:
                return httpx.Response(500, text="boom")
            self.listed.append(("add", body[0]["recipeId"], body[0]["recipeIncrementQuantity"]))
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected call: {method} {path}")

    @staticmethod
    def _is_note(entry: dict) -> bool:
        return (entry.get("title") or "").startswith(("Gæster:", "Guests:"))

    @property
    def notes(self) -> list[dict]:
        return [e for e in self.entries.values() if self._is_note(e)]

    @property
    def dishes(self) -> list[dict]:
        return [e for e in self.entries.values() if not self._is_note(e)]


@pytest.fixture
def mealie(monkeypatch, db):
    fake = FakeMealie()
    real = app_settings.MealieClient
    monkeypatch.setattr(app_settings, "MealieClient", lambda url, token: real(url, token, transport=httpx.MockTransport(fake.handler)))
    set_options(mealie_url="http://mealie:9000", mealie_public_url="https://mad.example.dk/", mealie_token="t")
    return fake


def web_recipe(db, title="Kylling i karry", url=URL, ingredients=None, servings=4, meal_types=("dinner",), household_id=None, **kwargs) -> Recipe:
    recipe = Recipe(
        household_id=household_id or household_of_admin(db).id,
        source="web",
        title=title,
        meal_types=list(meal_types),
        servings=servings,
        ingredients=ingredients or [{"name": "kyllingebryst", "quantity": 600, "unit": "g", "text": "600 g kyllingebryst"}],
        steps=["Steg kyllingen gylden."],
        tags=[],
        source_url=url,
        image_url="https://madsiden.dk/billeder/karry.jpg",
        **kwargs,
    )
    db.add(recipe)
    db.commit()
    return recipe


def only_real_recipes(db) -> None:
    """As a new installation has it, with none of MadPlan's own dishes to fall back on."""
    set_household(db, builtin_dishes=False)


def plan_of(client, days=1, **kwargs) -> dict:
    response = client.post("/api/v1/plans", json={"mode": "days_ahead", "days_ahead": days, "use_ai": False, **kwargs})
    assert response.status_code == 201, response.text
    return response.json()


def accept(client, meal_id: int) -> dict:
    return client.post(f"/api/v1/meals/{meal_id}/vote", json={"vote": "yes"}).json()


def send(client, plan_id: int) -> dict:
    response = client.post(f"/api/v1/plans/{plan_id}/send-to-mealie")
    assert response.status_code == 200, response.text
    return response.json()


def run_worker(db) -> dict:
    db.expire_all()
    counts = process_pending(db)
    db.commit()
    db.expire_all()
    return counts


def planned_and_accepted(client, db, days=1) -> dict:
    plan = plan_of(client, days)
    for meal in plan["meals"]:
        plan = accept(client, meal["id"])
    return plan


# Reading a page


def test_everything_comes_from_the_page():
    markup = page(
        name="Kylling &amp; karry",
        image=[{"@type": "ImageObject", "url": "https://cdn.dk/a.jpg?w=1&amp;h=2"}],
        steps=[{"@type": "HowToSection", "itemListElement": [{"@type": "HowToStep", "text": "1. Steg <b>kyllingen</b> gylden i en stor gryde."}]}, "2. Tilsæt løg, og lad retten simre et kvarter."],
        servings=["4", "4 portioner"],
    ).replace('{"@context"', '{"@graph": [{"@type": "WebPage"}, {"@context"', 1).replace("}</script>", "}]}</script>", 1)
    recipe = parse_recipe_page(markup, URL)
    assert recipe.title == "Kylling & karry"
    assert recipe.image_url == "https://cdn.dk/a.jpg?w=1&h=2"
    assert recipe.servings == 4
    assert recipe.steps == ["Steg kyllingen gylden i en stor gryde.", "Tilsæt løg, og lad retten simre et kvarter."]
    assert recipe.meal_types == ["dinner"]
    # The line as the page wrote it is kept, next to what prices are worked out from.
    assert recipe.ingredients[0] == {"name": "kyllingebryst", "quantity": 600, "unit": "g", "text": "600 g kyllingebryst (i tern)"}


@pytest.mark.parametrize(
    ("markup", "reason"),
    [
        ("<html><body>Bare en artikel om lasagne.</body></html>", "no_recipe"),
        (page(name=None), "no_title"),
        (page(image=None), "no_image"),
        (page(ingredients=None), "no_ingredients"),
        (page(ingredients=("kun én ting",)), "no_ingredients"),
        (page(steps=None), "no_steps"),
        (page(steps=["Bland."]), "no_steps"),
        (page(servings=None), "no_servings"),
        (page(category="Tilbehør"), "not_a_meal"),
    ],
)
def test_a_page_without_all_of_it_is_refused(markup, reason):
    with pytest.raises(PageRefused) as exc:
        parse_recipe_page(markup, URL)
    assert exc.value.reason == reason


def test_a_side_dish_that_is_also_dinner_is_a_meal():
    assert parse_recipe_page(page(category=["Tilbehør", "Aftensmad"]), URL).meal_types == ["dinner"]


def test_broken_structured_data_is_not_a_crash():
    with pytest.raises(PageRefused) as exc:
        parse_recipe_page('<script type="application/ld+json">{"@type": "Recipe", </script>', URL)
    assert exc.value.reason == "no_recipe"


@pytest.mark.parametrize(("url", "label"), [("https://www.arla.dk/opskrift/x", "arla.dk"), ("https://madensverden.dk/x/", "madensverden.dk"), (None, None)])
def test_the_button_is_named_after_the_domain(url, label):
    assert domain_of(url) == label


def test_a_page_robots_forbids_is_not_even_fetched(web):
    web.pages["https://madsiden.dk/robots.txt"] = httpx.Response(200, text="User-agent: *\nDisallow: /opskrift/", headers={"content-type": "text/plain"})
    web.serve(URL)
    with pytest.raises(PageRefused) as exc:
        read(web)
    assert exc.value.reason == "not_allowed"
    assert URL not in web.requests


def test_a_site_refusing_to_show_its_robots_is_not_a_yes(web):
    web.pages["https://madsiden.dk/robots.txt"] = httpx.Response(403, text="no")
    web.serve(URL)
    with pytest.raises(PageRefused):
        read(web)
    assert URL not in web.requests


def test_a_site_whose_terms_forbid_it_stays_blocked(web):
    assert policy.legal_block_reason("https://minetilbud.dk/opskrift")
    with pytest.raises(PageRefused) as exc:
        read(web, "https://minetilbud.dk/opskrift")
    assert exc.value.reason == "not_allowed"
    assert web.requests == []


def test_an_internal_address_is_never_contacted(web):
    with pytest.raises(PageRefused):
        read(web, "https://intern.local/opskrift")
    assert web.requests == []


def test_a_redirect_to_another_site_is_refused(web):
    web.pages[URL] = httpx.Response(302, headers={"location": "https://andensite.dk/x"})
    web.serve("https://andensite.dk/x")
    with pytest.raises(PageRefused):
        read(web)
    assert "https://andensite.dk/x" not in web.requests


def test_a_redirect_to_the_sites_www_address_is_followed_and_kept(web):
    web.pages[URL] = httpx.Response(301, headers={"location": "https://www.madsiden.dk/opskrift/kylling-i-karry/"})
    web.serve("https://www.madsiden.dk/opskrift/kylling-i-karry/")
    assert read(web).url == "https://www.madsiden.dk/opskrift/kylling-i-karry/"


def test_a_redirect_inwards_on_the_same_site_is_not_followed(web):
    """The allowlist lets the site's own www address through. It can still point somewhere internal."""
    inside = "https://www.madsiden.dk/intern"
    web.pages[URL] = httpx.Response(302, headers={"location": inside})
    web.serve(inside)
    with pytest.raises(PageRefused) as exc:
        read(web)
    assert exc.value.reason == "not_allowed"
    assert inside not in web.requests


def test_robots_redirecting_inwards_is_not_followed(web):
    web.pages["https://madsiden.dk/robots.txt"] = httpx.Response(302, headers={"location": "https://intern.local/robots.txt"})
    web.serve(URL)
    with pytest.raises(PageRefused):
        read(web)
    assert "https://intern.local/robots.txt" not in web.requests


def test_a_dead_link_is_refused(web):
    with pytest.raises(PageRefused) as exc:
        read(web, "https://madsiden.dk/findes-ikke")
    assert exc.value.reason == "unreachable"


# Searching. The model gives addresses, and can give nothing else


def found(*urls):
    return text_message(json.dumps({"pages": [{"url": url, "meal_type": "dinner"} for url in urls]}))


def search(client, **body):
    return client.post("/api/v1/new-recipes/search", json=body)


def test_the_model_can_only_give_an_address():
    assert set(FoundPage.model_fields) == {"url", "meal_type"}


def test_a_recipe_holds_what_the_page_says_and_nothing_from_the_model(admin_client, db, web, fake_openai):  # noqa: F811
    add_offer(db, "netto", "Kyllingebryst 600 g", 45, pack_quantity=600, pack_unit="g")
    web.serve(URL)
    # A model trying to add a recipe of its own: everything but the address is thrown away.
    fake_openai([text_message(json.dumps({"pages": [{"url": URL, "meal_type": "dinner", "title": "Opdigtet", "ingredients": ["luft"]}]}))])
    result = search(admin_client).json()
    assert result["found"] == 1
    db.expire_all()
    recipe = db.scalar(select(Recipe).where(Recipe.source == "web"))
    assert recipe.title == "Kylling i karry"
    assert recipe.image_url == "https://madsiden.dk/billeder/karry.jpg"
    assert [line["text"] for line in recipe.ingredients] == ["600 g kyllingebryst (i tern)", "1 løg", "4 dl ris"]
    assert recipe.steps[0] == "Steg kyllingen gylden i en stor gryde."
    assert recipe.source_url == URL


def test_an_address_the_model_made_up_gives_nothing(admin_client, db, web, fake_openai):  # noqa: F811
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    fake_openai([found("https://madsiden.dk/opdigtet")])
    result = search(admin_client).json()
    assert (result["found"], result["refused_why"]) == (0, {"unreachable": 1})
    assert db.scalar(select(Recipe).where(Recipe.source == "web")) is None


def test_pages_without_a_picture_are_counted_and_not_kept(admin_client, db, web, fake_openai):  # noqa: F811
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    web.serve(URL, page(image=None))
    fake_openai([found(URL)])
    assert search(admin_client).json()["refused_why"] == {"no_image": 1}
    assert db.scalar(select(Recipe).where(Recipe.source == "web")) is None


@pytest.mark.parametrize(
    "markup",
    [
        page(ingredients=("600 g kylling", "100 g hasselnødder")),
        # Left out of the name, and still on the line.
        page(ingredients=("600 g kylling", "1 løg, evt. med hasselnødder")),
        page(steps=["Steg kyllingen, og drys med hasselnødder til sidst."]),
        page(name="Kylling med nøddesovs"),
    ],
)
def test_a_page_with_an_allergen_is_not_kept_wherever_it_says_so(admin_client, db, web, fake_openai, markup):  # noqa: F811
    set_household(db, allergies=["nødder"])
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    web.serve(URL, markup)
    fake_openai([found(URL)])
    assert search(admin_client).json()["refused_why"] == {"allergy": 1}
    assert db.scalar(select(Recipe).where(Recipe.source == "web")) is None


def test_a_page_the_diet_rules_out_is_not_kept(admin_client, db, web, fake_openai):  # noqa: F811
    set_household(db, diet_mode="vegetarian")
    add_offer(db, "netto", "Kartofler 2 kg", 15)
    web.serve(URL)
    fake_openai([found(URL)])
    assert search(admin_client).json()["refused_why"] == {"diet": 1}


def test_a_recipe_is_only_found_once(admin_client, db, web, fake_openai):  # noqa: F811
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    web.serve(URL)
    other = "https://andensite.dk/samme-ret"
    web.serve(other, page())
    fake_openai([found(URL, URL + "/", other)])
    result = search(admin_client).json()
    assert result["found"] == 1
    assert result["refused_why"] == {"already_known": 2}


def test_what_someone_said_no_to_is_not_found_again(admin_client, db, web, fake_openai):  # noqa: F811
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    recipe = web_recipe(db)
    admin_client.post(f"/api/v1/new-recipes/{recipe.id}/dismiss")
    web.serve(URL)
    fake_openai([found(URL)])
    assert search(admin_client).json()["found"] == 0
    db.expire_all()
    assert db.get(Recipe, recipe.id).dismissed_at is not None


def test_no_offers_means_no_search_and_no_cost(admin_client, db, web, fake_openai):  # noqa: F811
    model = fake_openai([found(URL)])
    assert search(admin_client).json()["no_offers"] is True
    assert model.calls == []


def test_no_addresses_at_all_is_reported_so_web_search_can_be_switched_on(admin_client, db, web, fake_openai):  # noqa: F811
    # An AI Task that may not search the web honestly finds nothing.
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    fake_openai([found()])
    result = search(admin_client).json()
    assert (result["found"], result["refused"], result["links"]) == (0, 0, 0)


def test_the_search_is_told_what_is_on_offer_and_what_must_not_be_in_it(admin_client, db, web, fake_openai):  # noqa: F811
    set_household(db, allergies=["nødder"])
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    add_offer(db, "bilka", "Fladskærms-TV 55 tommer", 3999, pre_price=5999)
    web_recipe(db, title="Min gamle ret", url="https://madsiden.dk/gammel")
    model = fake_openai([found()])
    search(admin_client)
    prompt = model.calls[0]["instructions"]
    assert "Kyllingebryst 600 g" in prompt
    assert "TV" not in prompt
    assert "nødder" in prompt
    assert "Min gamle ret" in prompt
    assert "write recipes" in prompt
    assert "Search the web" in prompt


def test_a_search_without_a_key_says_so_and_the_page_still_works(admin_client, db, web):
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    assert (search(admin_client).status_code, search(admin_client).json()["detail"]) == (409, "ai_unavailable:not_configured")
    assert admin_client.get("/api/v1/new-recipes").json()["search_blocked"] == "not_configured"


def test_a_link_someone_found_works_without_ai(admin_client, db, web):
    web.serve(URL)
    response = admin_client.post("/api/v1/new-recipes/from-link", json={"url": URL})
    assert response.status_code == 201
    assert response.json()["link"] == {"kind": "web", "url": URL, "label": "madsiden.dk"}


def test_a_link_to_an_incomplete_page_is_refused_with_the_reason(admin_client, web):
    web.serve(URL, page(steps=None))
    response = admin_client.post("/api/v1/new-recipes/from-link", json={"url": URL})
    assert (response.status_code, response.json()["detail"]) == (409, "page_no_steps")


def test_a_link_to_something_declined_earlier_brings_it_back(admin_client, db, web):
    recipe = web_recipe(db)
    admin_client.post(f"/api/v1/new-recipes/{recipe.id}/dismiss")
    web.serve(URL)
    assert admin_client.post("/api/v1/new-recipes/from-link", json={"url": URL}).json()["id"] == recipe.id
    db.expire_all()
    assert db.get(Recipe, recipe.id).dismissed_at is None


# The page


def test_the_page_shows_what_is_on_offer_with_real_prices(admin_client, db):
    add_offer(db, "netto", "Kyllingebryst 600 g", 45, pre_price=70, pack_quantity=600, pack_unit="g")
    web_recipe(db)
    web_recipe(db, title="Ret uden tilbud", url="https://madsiden.dk/anden", ingredients=[{"name": "safran", "quantity": 1, "unit": "g"}, {"name": "kokosmælk", "quantity": 1, "unit": "stk"}])
    data = admin_client.get("/api/v1/new-recipes").json()
    assert [r["title"] for r in data["recipes"]] == ["Kylling i karry"]
    assert data["waiting_for_offers"] == 1
    card = data["recipes"][0]
    assert card["link"]["label"] == "madsiden.dk"
    assert card["image_url"] == "https://madsiden.dk/billeder/karry.jpg"
    assert card["offers"][0]["offer"] == "Kyllingebryst 600 g"
    assert card["offers"][0]["store"] == "Netto"
    assert card["offers"][0]["price"] == 45
    assert card["offers"][0]["before"] == 70


def test_a_price_only_says_before_when_the_leaflet_did(admin_client, db):
    add_offer(db, "netto", "Kyllingebryst 600 g", 45, pack_quantity=600, pack_unit="g")
    web_recipe(db)
    assert admin_client.get("/api/v1/new-recipes").json()["recipes"][0]["offers"][0]["before"] is None


def test_salt_on_offer_is_no_reason_for_a_dish(admin_client, db):
    add_offer(db, "netto", "Salt 1 kg", 5)
    web_recipe(db, ingredients=[{"name": "salt", "quantity": 1, "unit": "tsk"}, {"name": "safran", "quantity": 1, "unit": "g"}])
    assert admin_client.get("/api/v1/new-recipes").json()["recipes"] == []


def test_what_is_already_in_mealie_is_not_new(admin_client, db):
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    web_recipe(db, mealie_slug="kylling", mealie_id="1")
    assert admin_client.get("/api/v1/new-recipes").json()["recipes"] == []


def test_an_allergy_added_later_hides_a_recipe_at_once(admin_client, db):
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    web_recipe(db)
    set_household(db, allergies=["kylling"])
    assert admin_client.get("/api/v1/new-recipes").json()["recipes"] == []


def test_no_thanks_can_be_taken_back(admin_client, db):
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    recipe = web_recipe(db)
    admin_client.post(f"/api/v1/new-recipes/{recipe.id}/dismiss")
    assert admin_client.get("/api/v1/new-recipes").json()["recipes"] == []
    admin_client.delete(f"/api/v1/new-recipes/{recipe.id}/dismiss")
    assert len(admin_client.get("/api/v1/new-recipes").json()["recipes"]) == 1


def test_it_can_be_switched_off_per_person(db):
    make_user(db, "stille", settings={"features": {"new_recipes": False}})
    assert login_as("stille").get("/api/v1/new-recipes").status_code == 403


def test_another_households_recipes_stay_theirs(admin_client, db, mealie):
    other = Household(name="Naboen", settings={})
    db.add(other)
    db.commit()
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    theirs = web_recipe(db, household_id=other.id)
    assert admin_client.get("/api/v1/new-recipes").json()["recipes"] == []
    for call in (admin_client.post(f"/api/v1/new-recipes/{theirs.id}/to-mealie"), admin_client.post(f"/api/v1/new-recipes/{theirs.id}/dismiss"), admin_client.delete(f"/api/v1/new-recipes/{theirs.id}/dismiss")):
        assert call.status_code == 404
    # Reading Mealie's planner when a plan is made is fine. Nothing may be written.
    assert [c for c in mealie.calls if not c.startswith("GET ")] == []


# Adding a recipe to Mealie from the page


def test_adding_imports_the_page_itself(admin_client, db, mealie):
    recipe = web_recipe(db)
    mealie.imports[URL] = good_remote()
    response = admin_client.post(f"/api/v1/new-recipes/{recipe.id}/to-mealie")
    assert response.status_code == 200, response.text
    assert response.json()["link"] == {"kind": "mealie", "url": "https://mad.example.dk/g/home/r/kylling-i-karry", "label": "Mealie"}
    assert "kylling-i-karry" in mealie.recipes
    # The recipe alone: the calendar and the shopping list are for the plan.
    assert mealie.entries == {} and mealie.listed == []


def test_a_recipe_already_in_mealie_is_linked_and_not_imported_again(admin_client, db, mealie):
    recipe = web_recipe(db)
    mealie.recipes["kylling-i-karry"] = good_remote()
    assert admin_client.post(f"/api/v1/new-recipes/{recipe.id}/to-mealie").json()["link"]["kind"] == "mealie"
    assert "POST /api/recipes/create/url" not in mealie.calls


@pytest.mark.parametrize(
    ("remote", "code"),
    [
        (good_remote(lines=("Could not detect ingredients", "x")), "import_unreadable"),
        (good_remote(lines=("kun én",)), "import_unreadable"),
        (good_remote(steps=()), "import_unreadable"),
        (good_remote(image=None), "import_no_image"),
        (good_remote(image="no image"), "import_no_image"),
        (good_remote(lines=("600 g kylling", "100 g hasselnødder")), "import_allergy"),
    ],
)
def test_an_import_that_is_not_all_there_is_taken_out_of_mealie_again(admin_client, db, mealie, remote, code):
    set_household(db, allergies=["nødder"])
    recipe = web_recipe(db)
    mealie.imports[URL] = remote
    response = admin_client.post(f"/api/v1/new-recipes/{recipe.id}/to-mealie")
    assert (response.status_code, response.json()["detail"]) == (409, code)
    assert mealie.deleted == [remote["slug"]]
    db.expire_all()
    assert db.get(Recipe, recipe.id).mealie_slug is None


def test_a_page_mealie_refuses_outright_is_said_so(admin_client, db, mealie):
    recipe = web_recipe(db)
    assert admin_client.post(f"/api/v1/new-recipes/{recipe.id}/to-mealie").json()["detail"] == "import_unreadable"


def test_an_allergy_added_since_stops_the_import(admin_client, db, mealie):
    recipe = web_recipe(db)
    set_household(db, allergies=["kylling"])
    assert admin_client.post(f"/api/v1/new-recipes/{recipe.id}/to-mealie").json()["detail"] == "import_allergy"
    # Reading Mealie's planner when a plan is made is fine. Nothing may be written.
    assert [c for c in mealie.calls if not c.startswith("GET ")] == []


def test_a_recipe_in_mealie_with_an_allergen_is_not_taken_for_this_one(admin_client, db, mealie):
    set_household(db, allergies=["nødder"])
    recipe = web_recipe(db)
    mealie.recipes["kylling-i-karry"] = good_remote(lines=("600 g kylling", "100 g hasselnødder"))
    mealie.imports[URL] = good_remote(title="Kylling i karry fra siden")
    assert admin_client.post(f"/api/v1/new-recipes/{recipe.id}/to-mealie").status_code == 200
    db.expire_all()
    assert db.get(Recipe, recipe.id).mealie_slug == "kylling-i-karry-fra-siden"


def test_without_mealie_it_says_so(admin_client, db):
    recipe = web_recipe(db)
    assert admin_client.post(f"/api/v1/new-recipes/{recipe.id}/to-mealie").json()["detail"] == "mealie_not_connected"


def test_mealie_being_down_is_not_called_a_bad_recipe(admin_client, db, monkeypatch):
    real = app_settings.MealieClient

    def down(request):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(app_settings, "MealieClient", lambda url, token: real(url, token, transport=httpx.MockTransport(down)))
    set_options(mealie_url="http://mealie:9000", mealie_token="t")
    recipe = web_recipe(db)
    response = admin_client.post(f"/api/v1/new-recipes/{recipe.id}/to-mealie")
    assert (response.status_code, response.json()["detail"]) == (502, "mealie_error")


# What may be planned


def test_a_new_installation_does_not_use_madplans_own_dishes():
    assert HouseholdSettings().builtin_dishes is False


def test_without_any_real_recipes_there_is_no_plan_and_it_says_why(admin_client, db):
    only_real_recipes(db)
    response = admin_client.post("/api/v1/plans", json={"mode": "days_ahead", "days_ahead": 1, "use_ai": False})
    assert (response.status_code, response.json()["detail"]) == (409, "no_recipes")
    assert db.scalar(select(MealPlan)) is None


def test_asking_for_madplans_own_dishes_works_and_they_have_no_link(admin_client, db):
    plan = plan_of(admin_client)
    assert plan["meals"][0]["recipe_source"] == "builtin"
    assert plan["meals"][0]["link"] is None


def test_what_a_model_wrote_earlier_is_not_suggested_any_more(admin_client, db):
    only_real_recipes(db)
    db.add(Recipe(household_id=household_of_admin(db).id, source="ai", title="Opdigtet gryde", meal_types=["dinner"], servings=4, ingredients=[{"name": "kartofler"}], steps=["Kog."], tags=[]))
    db.commit()
    assert admin_client.post("/api/v1/plans", json={"mode": "days_ahead", "days_ahead": 1, "use_ai": False}).status_code == 409


def test_what_someone_kept_in_mealie_from_back_then_still_counts(admin_client, db, mealie):
    only_real_recipes(db)
    db.add(Recipe(household_id=household_of_admin(db).id, source="ai", title="Gammel ret", meal_types=["dinner"], servings=4, ingredients=[{"name": "kartofler"}], steps=["Kog."], tags=[], mealie_slug="gammel-ret", mealie_id="1"))
    db.commit()
    assert plan_of(admin_client)["meals"][0]["title"] == "Gammel ret"


def test_a_dish_from_the_web_shows_the_site_on_a_button(admin_client, db):
    only_real_recipes(db)
    web_recipe(db, url="https://www.arla.dk/opskrifter/karry/")
    assert plan_of(admin_client)["meals"][0]["link"] == {"kind": "web", "url": "https://www.arla.dk/opskrifter/karry/", "label": "arla.dk"}


def test_a_dish_in_mealie_shows_only_the_mealie_button(admin_client, db, mealie):
    only_real_recipes(db)
    stored = get_integrations(db, household_of_admin(db).id)
    save_integrations(db, household_of_admin(db).id, stored.model_copy(update={"mealie_group_slug": "home"}))
    db.commit()
    web_recipe(db, mealie_slug="kylling-i-karry", mealie_id="1")
    assert plan_of(admin_client)["meals"][0]["link"] == {"kind": "mealie", "url": "https://mad.example.dk/g/home/r/kylling-i-karry", "label": "Mealie"}


def test_someone_a_plan_is_shared_with_does_not_get_the_address_of_our_mealie(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db, mealie_slug="kylling-i-karry", mealie_id="1")
    plan = plan_of(admin_client)
    other = Household(name="Naboen", settings={})
    db.add(other)
    db.commit()
    guest = make_user(db, "gaest", household_id=other.id)
    admin_client.post(f"/api/v1/plans/{plan['id']}/share", json={"user_ids": [guest.id]})
    seen = login_as("gaest").get(f"/api/v1/plans/{plan['id']}").json()
    assert "mad.example.dk" not in json.dumps(seen["meals"])
    assert seen["mealie"] is None
    assert login_as("gaest").post(f"/api/v1/plans/{plan['id']}/send-to-mealie").status_code == 403
    assert login_as("gaest").get(f"/api/v1/plans/{plan['id']}/mealie-entries").status_code == 403


def test_the_same_dinner_is_not_served_twice_and_the_plan_says_it_is_short(admin_client, db):
    only_real_recipes(db)
    web_recipe(db)
    plan = plan_of(admin_client, 3)
    assert [m["title"] for m in plan["meals"]] == ["Kylling i karry"]
    assert plan["missing_slots"] == 2


def test_a_declined_recipe_does_not_end_up_on_a_plan(admin_client, db):
    only_real_recipes(db)
    web_recipe(db, title="Vil ikke have")
    keep = web_recipe(db, title="Vil gerne have", url="https://madsiden.dk/ja")
    admin_client.post(f"/api/v1/new-recipes/{keep.id - 1}/dismiss")
    assert [m["title"] for m in plan_of(admin_client)["meals"]] == ["Vil gerne have"]


def test_a_plan_with_too_few_recipes_searches_for_more(admin_client, db, web, fake_openai):  # noqa: F811
    only_real_recipes(db)
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    web.serve(URL)
    model = fake_openai([found(URL)])
    plan = plan_of(admin_client, 1, use_ai=True)
    assert len(model.calls) == 1
    assert plan["ai_used"] is True
    assert plan["meals"][0]["link"]["label"] == "madsiden.dk"


def test_a_plan_with_plenty_to_choose_from_spends_nothing(admin_client, db, web, fake_openai):  # noqa: F811
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    model = fake_openai([found(URL)])
    assert plan_of(admin_client, 1, use_ai=True)["ai_used"] is False
    assert model.calls == []


def test_the_plan_gets_made_when_the_search_fails(admin_client, db, web, fake_openai):  # noqa: F811
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    fake_openai([text_message("not json at all")])
    set_household(db, default_meal_types=["dinner"])
    plan = plan_of(admin_client, 31, use_ai=True)
    assert plan["ai_note"] == "call_failed"
    assert plan["ai_used"] is False
    assert plan["meals"]


def test_asking_for_another_dish_finds_a_real_one(admin_client, db, web, fake_openai):  # noqa: F811
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    plan = plan_of(admin_client)
    web.serve(URL)
    fake_openai([found(URL)])
    updated = admin_client.post(f"/api/v1/meals/{plan['meals'][0]['id']}/ai-alternative").json()
    alternative = updated["meals"][0]["alternatives"][0]
    assert (alternative["title"], alternative["source"]) == ("Kylling i karry", "web")
    assert alternative["link"]["label"] == "madsiden.dk"


def test_the_assistant_is_told_not_to_write_recipes(admin_client, fake_openai):  # noqa: F811
    model = fake_openai([text_message("x")])
    admin_client.post("/api/v1/assistant", json={"message": "?", "mode": "direct"})
    assert "Never write out a recipe" in model.calls[0]["instructions"]


# Nothing reaches Mealie before the button


def test_a_yes_does_not_touch_mealie(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    mealie.imports[URL] = good_remote()
    plan = planned_and_accepted(admin_client, db)
    assert plan["meals"][0]["status"] == "accepted"
    assert plan["meals"][0]["sync_status"] == "none"
    assert run_worker(db) == {"synced": 0, "failed": 0}
    # Reading Mealie's planner when a plan is made is fine. Nothing may be written.
    assert [c for c in mealie.calls if not c.startswith("GET ")] == []


def test_yes_to_all_and_an_admin_deciding_do_not_touch_mealie_either(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    web_recipe(db, title="Laks i ovn", url="https://madsiden.dk/laks")
    plan = plan_of(admin_client, 2)
    assert admin_client.post(f"/api/v1/meals/{plan['meals'][0]['id']}/decide", json={"vote": "yes"}).status_code == 200
    admin_client.post(f"/api/v1/plans/{plan['id']}/accept-all")
    run_worker(db)
    # Reading Mealie's planner when a plan is made is fine. Nothing may be written.
    assert [c for c in mealie.calls if not c.startswith("GET ")] == []


def test_a_vote_from_home_assistant_does_not_touch_mealie(admin_client, db, mealie, client):
    only_real_recipes(db)
    web_recipe(db)
    plan = plan_of(admin_client)
    token = admin_client.post("/api/v1/me/tokens", json={"name": "ha"}).json()["token"]
    answer = client.post(f"/api/v1/ha/meals/{plan['meals'][0]['id']}/vote", json={"vote": "yes"}, headers={"Authorization": f"Bearer {token}"})
    assert answer.json()["status"] == "accepted"
    run_worker(db)
    # Reading Mealie's planner when a plan is made is fine. Nothing may be written.
    assert [c for c in mealie.calls if not c.startswith("GET ")] == []


def test_retry_cannot_be_used_to_send_before_the_button(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    plan = planned_and_accepted(admin_client, db)
    admin_client.post(f"/api/v1/meals/{plan['meals'][0]['id']}/retry-sync")
    run_worker(db)
    # Reading Mealie's planner when a plan is made is fine. Nothing may be written.
    assert [c for c in mealie.calls if not c.startswith("GET ")] == []


def test_the_button_sends_the_recipe_the_calendar_and_the_list(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    mealie.imports[URL] = good_remote()
    plan = planned_and_accepted(admin_client, db)
    assert plan["mealie"]["to_send"] == 1
    sent = send(admin_client, plan["id"])
    assert sent["meals"][0]["sync_status"] == "pending"
    assert sent["mealie"]["sending"] == 1
    # Reading Mealie's planner when a plan is made is fine. Nothing may be written.
    assert [c for c in mealie.calls if not c.startswith("GET ")] == []  # The worker does it.
    assert run_worker(db)["synced"] == 1
    assert list(mealie.recipes) == ["kylling-i-karry"]
    assert [e["recipeId"] for e in mealie.dishes] == ["id-kylling-i-karry"]
    assert mealie.listed == [("add", "id-kylling-i-karry", 0.5)]
    after = admin_client.get(f"/api/v1/plans/{plan['id']}").json()
    assert after["meals"][0]["sync_status"] == "synced"
    assert after["meals"][0]["link"]["kind"] == "mealie"
    assert after["mealie"]["to_send"] == 0


def test_only_what_is_accepted_goes(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    web_recipe(db, title="Laks i ovn", url="https://madsiden.dk/laks")
    plan = plan_of(admin_client, 2)
    plan = accept(admin_client, plan["meals"][0]["id"])
    title = plan["meals"][0]["title"]
    mealie.imports[next(r for r in db.scalars(select(Recipe).where(Recipe.source == "web")) if r.title == title).source_url] = good_remote(title=title)
    assert plan["mealie"]["undecided"] == 1
    send(admin_client, plan["id"])
    run_worker(db)
    assert [e["title"] for e in mealie.dishes] == [title]


def test_pressing_twice_changes_nothing(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    mealie.imports[URL] = good_remote()
    plan = planned_and_accepted(admin_client, db)
    send(admin_client, plan["id"])
    run_worker(db)
    send(admin_client, plan["id"])
    run_worker(db)
    assert len(mealie.dishes) == 1
    assert len(mealie.listed) == 1


def test_a_no_after_sending_waits_for_the_button_and_then_comes_out(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    mealie.imports[URL] = good_remote()
    plan = planned_and_accepted(admin_client, db)
    send(admin_client, plan["id"])
    run_worker(db)
    meal_id = plan["meals"][0]["id"]
    after_no = admin_client.post(f"/api/v1/meals/{meal_id}/vote", json={"vote": "no"}).json()
    assert after_no["mealie"]["to_send"] == 1
    run_worker(db)
    assert len(mealie.dishes) == 1  # Still there.
    send(admin_client, plan["id"])
    run_worker(db)
    assert mealie.dishes == []
    assert mealie.listed[-1] == ("remove", "id-kylling-i-karry", 0.5)
    assert admin_client.get(f"/api/v1/plans/{plan['id']}").json()["mealie"]["to_send"] == 0


def test_a_replaced_dish_takes_the_old_one_out_of_mealie(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    mealie.imports[URL] = good_remote()
    plan = planned_and_accepted(admin_client, db)
    send(admin_client, plan["id"])
    run_worker(db)
    laks_url = "https://madsiden.dk/laks"
    laks = web_recipe(db, title="Laks i ovn", url=laks_url)
    mealie.imports[laks_url] = good_remote(title="Laks i ovn")
    meal_id = plan["meals"][0]["id"]
    admin_client.post(f"/api/v1/meals/{meal_id}/replace", json={"recipe_id": laks.id})
    accepted = accept(admin_client, meal_id)
    assert accepted["mealie"]["to_send"] == 1
    send(admin_client, plan["id"])
    run_worker(db)
    assert [e["recipeId"] for e in mealie.dishes] == ["id-laks-i-ovn"]
    assert ("remove", "id-kylling-i-karry", 0.5) in mealie.listed
    assert ("add", "id-laks-i-ovn", 0.5) in mealie.listed


def test_a_skipped_day_comes_out_of_mealie_at_the_next_send(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    mealie.imports[URL] = good_remote()
    plan = planned_and_accepted(admin_client, db)
    send(admin_client, plan["id"])
    run_worker(db)
    day = plan["meals"][0]["date"]
    skipped = admin_client.patch(f"/api/v1/plans/{plan['id']}/days/{day}", json={"skipped": True}).json()["plan"]
    assert len(mealie.dishes) == 1
    assert skipped["mealie"]["to_send"] == 1
    send(admin_client, plan["id"])
    run_worker(db)
    assert mealie.dishes == []


def test_a_day_skipped_before_sending_is_simply_gone(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    plan = planned_and_accepted(admin_client, db)
    skipped = admin_client.patch(f"/api/v1/plans/{plan['id']}/days/{plan['meals'][0]['date']}", json={"skipped": True}).json()["plan"]
    assert skipped["meals"] == []
    assert skipped["missing_slots"] == 0


def test_a_failed_step_does_not_double_book_when_tried_again(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    mealie.imports[URL] = good_remote()
    plan = planned_and_accepted(admin_client, db)
    send(admin_client, plan["id"])
    mealie.fail_list = True
    assert run_worker(db)["failed"] == 1
    assert db.get(PlannedMeal, plan["meals"][0]["id"]).sync_status == "pending"
    mealie.fail_list = False
    assert run_worker(db)["synced"] == 1
    assert len(mealie.dishes) == 1
    assert len(mealie.listed) == 1


def test_a_refused_import_says_why_and_does_not_keep_trying(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    mealie.imports[URL] = good_remote(image=None)
    plan = planned_and_accepted(admin_client, db)
    send(admin_client, plan["id"])
    assert run_worker(db)["failed"] == 1
    meal = admin_client.get(f"/api/v1/plans/{plan['id']}").json()["meals"][0]
    assert (meal["sync_status"], meal["sync_error"]) == ("failed", "import_no_image")
    assert meal["link"]["kind"] == "web"
    assert mealie.recipes == {} and mealie.entries == {}
    calls = len(mealie.calls)
    run_worker(db)
    assert len(mealie.calls) == calls


def test_a_failed_meal_goes_again_with_the_button(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    mealie.imports[URL] = good_remote(image=None)
    plan = planned_and_accepted(admin_client, db)
    send(admin_client, plan["id"])
    run_worker(db)
    mealie.imports[URL] = good_remote()
    assert admin_client.get(f"/api/v1/plans/{plan['id']}").json()["mealie"]["to_send"] == 1
    send(admin_client, plan["id"])
    assert run_worker(db)["synced"] == 1


def test_madplans_own_dish_goes_on_the_calendar_by_name_only(admin_client, db, mealie):
    plan = planned_and_accepted(admin_client, db)
    send(admin_client, plan["id"])
    assert run_worker(db)["synced"] == 1  # FakeMealie fails the test if a recipe gets written.
    assert mealie.recipes == {}
    assert [e["title"] for e in mealie.dishes] == [plan["meals"][0]["title"]]
    assert mealie.dishes[0]["recipeId"] is None
    assert mealie.listed == []


def test_a_recipe_deleted_in_mealie_is_imported_again(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db, mealie_slug="kylling-i-karry", mealie_id="gammel")
    mealie.imports[URL] = good_remote()
    plan = planned_and_accepted(admin_client, db)
    send(admin_client, plan["id"])
    assert run_worker(db)["synced"] == 1
    assert [e["recipeId"] for e in mealie.dishes] == ["id-kylling-i-karry"]


def test_an_allergen_that_came_into_a_mealie_recipe_later_stays_off_the_list(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db, mealie_slug="kylling-i-karry", mealie_id="id-kylling-i-karry")
    mealie.recipes["kylling-i-karry"] = good_remote(lines=("600 g kylling", "100 g hasselnødder"))
    plan = planned_and_accepted(admin_client, db)
    set_household(db, allergies=["nødder"])
    send(admin_client, plan["id"])
    assert run_worker(db)["failed"] == 1
    assert db.get(PlannedMeal, plan["meals"][0]["id"]).sync_error == "import_allergy"
    assert mealie.listed == [] and mealie.entries == {}


def test_the_button_says_when_it_cannot_send(admin_client, db):
    only_real_recipes(db)
    web_recipe(db)
    plan = planned_and_accepted(admin_client, db)
    response = admin_client.post(f"/api/v1/plans/{plan['id']}/send-to-mealie")
    assert (response.status_code, response.json()["detail"]) == (409, "mealie_not_connected")


def test_a_meal_from_yesterday_is_not_sent_now(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    plan = planned_and_accepted(admin_client, db)
    meal = db.get(PlannedMeal, plan["meals"][0]["id"])
    meal.date = local_today() - timedelta(days=1)
    db.commit()
    assert send(admin_client, plan["id"])["mealie"]["to_send"] == 0
    run_worker(db)
    # Reading Mealie's planner when a plan is made is fine. Nothing may be written.
    assert [c for c in mealie.calls if not c.startswith("GET ")] == []


def test_a_meal_sent_by_the_earlier_version_is_left_alone(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db, mealie_slug="kylling-i-karry", mealie_id="id-kylling-i-karry")
    plan = planned_and_accepted(admin_client, db)
    meal = db.get(PlannedMeal, plan["meals"][0]["id"])
    meal.mealie_entry_id, meal.sync_status = "7", "synced"
    db.commit()
    assert admin_client.get(f"/api/v1/plans/{plan['id']}").json()["mealie"]["to_send"] == 0


# Guests


def send_with_guests(client, db, mealie, adults=2, children=1) -> dict:
    only_real_recipes(db)
    web_recipe(db)
    mealie.imports[URL] = good_remote()
    plan = plan_of(client)
    day = plan["meals"][0]["date"]
    plan = client.patch(f"/api/v1/plans/{plan['id']}/days/{day}", json={"guest_adults": adults, "guest_children": children}).json()["plan"]
    plan = accept(client, plan["meals"][0]["id"])
    send(client, plan["id"])
    run_worker(db)
    return plan


def test_guests_get_a_note_of_their_own_in_mealies_planner(admin_client, db, mealie):
    """On the entry with the recipe it would not show: Mealie's planner displays only the recipe."""
    send_with_guests(admin_client, db, mealie)
    assert len(mealie.dishes) == 1
    assert mealie.dishes[0]["text"] == "Fra MadPlan"
    assert [n["title"] for n in mealie.notes] == ["Gæster: 2 voksne og 1 barn"]
    note = mealie.notes[0]
    assert note["text"] == "I alt 4 voksne og 1 barn: Kylling i karry\n\nFra MadPlan"
    assert note["entryType"] == "dinner"
    assert note["recipeId"] is None
    assert note["date"] == mealie.dishes[0]["date"]


def test_the_note_follows_the_persons_language(admin_client, db, mealie):
    admin_client.headers["X-MadPlan-Language"] = "en"
    send_with_guests(admin_client, db, mealie, adults=1, children=2)
    assert mealie.notes[0]["title"] == "Guests: 1 adult and 2 children"
    assert mealie.dishes[0]["text"] == "From MadPlan"


def test_more_people_on_a_single_meal_are_guests_too(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    mealie.imports[URL] = good_remote()
    plan = plan_of(admin_client)
    meal_id = plan["meals"][0]["id"]
    admin_client.patch(f"/api/v1/meals/{meal_id}/people", json={"adults": 3, "children": 0})
    accept(admin_client, meal_id)
    send(admin_client, plan["id"])
    run_worker(db)
    assert [n["title"] for n in mealie.notes] == ["Gæster: 1 voksen"]


def test_no_guests_no_note(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    mealie.imports[URL] = good_remote()
    plan = planned_and_accepted(admin_client, db)
    send(admin_client, plan["id"])
    run_worker(db)
    assert mealie.notes == []


def test_more_guests_change_the_note_and_the_amounts_at_the_next_send(admin_client, db, mealie):
    plan = send_with_guests(admin_client, db, mealie, adults=2, children=0)
    assert mealie.listed == [("add", "id-kylling-i-karry", 1.0)]
    day = plan["meals"][0]["date"]
    changed = admin_client.patch(f"/api/v1/plans/{plan['id']}/days/{day}", json={"guest_adults": 6, "guest_children": 0}).json()["plan"]
    assert changed["mealie"]["to_send"] == 1
    assert [n["title"] for n in mealie.notes] == ["Gæster: 2 voksne"]  # Not before the button.
    send(admin_client, plan["id"])
    run_worker(db)
    assert [n["title"] for n in mealie.notes] == ["Gæster: 6 voksne"]
    assert len(mealie.dishes) == 1
    assert mealie.dishes[0]["recipeScale"] == 2.0
    assert mealie.listed == [("add", "id-kylling-i-karry", 1.0), ("remove", "id-kylling-i-karry", 1.0), ("add", "id-kylling-i-karry", 2.0)]


def test_guests_cancelling_takes_the_note_out(admin_client, db, mealie):
    plan = send_with_guests(admin_client, db, mealie)
    day = plan["meals"][0]["date"]
    admin_client.patch(f"/api/v1/plans/{plan['id']}/days/{day}", json={"guest_adults": 0, "guest_children": 0})
    send(admin_client, plan["id"])
    run_worker(db)
    assert mealie.notes == []
    assert len(mealie.dishes) == 1


def test_a_rejected_meal_takes_its_note_with_it(admin_client, db, mealie):
    plan = send_with_guests(admin_client, db, mealie)
    admin_client.post(f"/api/v1/meals/{plan['meals'][0]['id']}/vote", json={"vote": "no"})
    send(admin_client, plan["id"])
    run_worker(db)
    assert mealie.entries == {}


# What Mealie has planned already


def planned_elsewhere(mealie, date, title="Lasagne", slug="lasagne", entry_type="dinner"):
    mealie.next_entry += 1
    mealie.entries[mealie.next_entry] = {"id": mealie.next_entry, "date": date, "entryType": entry_type, "title": "", "text": "", "recipe": {"slug": slug, "name": title} if slug else None}
    if not slug:
        mealie.entries[mealie.next_entry]["title"] = title


def test_the_plan_page_can_see_what_mealie_has_planned_already(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    plan = plan_of(admin_client)
    day = plan["meals"][0]["date"]
    planned_elsewhere(mealie, day)
    planned_elsewhere(mealie, day, title="Rester", slug=None, entry_type="lunch")
    stored = get_integrations(db, household_of_admin(db).id)
    save_integrations(db, household_of_admin(db).id, stored.model_copy(update={"mealie_group_slug": "home"}))
    db.commit()
    entries = [{k: v for k, v in e.items() if k != "id"} for e in admin_client.get(f"/api/v1/plans/{plan['id']}/mealie-entries").json()]
    assert entries == [
        {"kind": "recipe", "date": day, "meal_type": "dinner", "title": "Lasagne", "mealie_slug": "lasagne", "url": "https://mad.example.dk/g/home/r/lasagne", "from_madplan": False},
        {"kind": "note", "date": day, "meal_type": "lunch", "title": "Rester", "mealie_slug": None, "url": None, "from_madplan": False},
    ]


def test_a_plans_own_entries_and_notes_are_not_reported_back_to_it(admin_client, db, mealie):
    plan = send_with_guests(admin_client, db, mealie)
    assert len(mealie.entries) == 2
    assert admin_client.get(f"/api/v1/plans/{plan['id']}/mealie-entries").json() == []


def test_another_plans_dish_counts_as_planned_but_its_note_does_not(admin_client, db, mealie):
    first = send_with_guests(admin_client, db, mealie)
    second = plan_of(admin_client)
    entries = admin_client.get(f"/api/v1/plans/{second['id']}/mealie-entries").json()
    assert [e["title"] for e in entries] == [first["meals"][0]["title"]]


def test_no_mealie_is_no_list_and_no_error(admin_client, db):
    plan = plan_of(admin_client)
    assert admin_client.get(f"/api/v1/plans/{plan['id']}/mealie-entries").json() == []


def test_someone_elses_plan_stays_hidden(admin_client, db, mealie):
    plan = plan_of(admin_client)
    other = Household(name="Naboen", settings={})
    db.add(other)
    db.commit()
    make_user(db, "nabo", household_id=other.id)
    neighbour = login_as("nabo")
    assert neighbour.get(f"/api/v1/plans/{plan['id']}/mealie-entries").status_code == 404
    assert neighbour.post(f"/api/v1/plans/{plan['id']}/send-to-mealie").status_code == 404


# Keeping up with Mealie


def test_a_recipe_deleted_in_mealie_loses_its_button_and_its_place(admin_client, db, mealie):
    only_real_recipes(db)
    from_web = web_recipe(db, mealie_slug="kylling-i-karry", mealie_id="1")
    pulled = web_recipe(db, title="Fra Mealie", url=None, mealie_slug="fra-mealie", mealie_id="2")
    pulled.source = "mealie"
    db.commit()
    mealie.recipes["noget-andet"] = good_remote(title="Noget andet")
    client = app_settings.mealie_client(db, household_of_admin(db).id)
    with client:
        pull_recipes(db, household_of_admin(db).id, client)
    db.expire_all()
    assert db.get(Recipe, pulled.id).mealie_slug is None
    assert db.get(Recipe, from_web.id).mealie_slug is None
    # Taken out on purpose, so it is not offered as a new idea either.
    assert db.get(Recipe, from_web.id).dismissed_at is not None
    assert [m["title"] for m in plan_of(admin_client)["meals"]] == ["Noget andet"]


def test_an_empty_answer_from_mealie_does_not_clear_every_link(db, mealie):
    recipe = web_recipe(db, mealie_slug="kylling-i-karry", mealie_id="1")
    client = app_settings.mealie_client(db, household_of_admin(db).id)
    with client:
        pull_recipes(db, household_of_admin(db).id, client)
    db.expire_all()
    assert db.get(Recipe, recipe.id).mealie_slug == "kylling-i-karry"


def test_the_placeholder_mealie_writes_is_seen_for_what_it_is():
    assert is_usable_import(good_remote(lines=("Could not detect ingredients", "x"))) is False
    assert is_usable_import(good_remote()) is True


# The worker


def test_the_schedule_searches_and_the_page_then_has_something(admin_client, db, web, fake_openai):  # noqa: F811
    add_offer(db, "netto", "Kyllingebryst 600 g", 45, pack_quantity=600, pack_unit="g")
    web.serve(URL)
    fake_openai([found(URL)])
    job_recipe_search(db)
    db.commit()
    assert [r["title"] for r in admin_client.get("/api/v1/new-recipes").json()["recipes"]] == ["Kylling i karry"]


def test_nobody_wanting_it_means_no_search(db, web, fake_openai):  # noqa: F811
    for member in db.scalars(select(User)):
        member.settings = {"features": {"new_recipes": False}}
    db.commit()
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    model = fake_openai([found(URL)])
    job_recipe_search(db)
    assert model.calls == []


def test_the_schedule_can_be_switched_off(db, monkeypatch):
    set_options(ai_task_entity="ai_task.madplan")
    ran = []
    monkeypatch.setattr("app.worker.job_recipe_search", lambda _: ran.append(1))
    monkeypatch.setattr("app.worker.crawl_all", lambda *args: [])
    # Not before the first crawl, as there is nothing to search around until then.
    run_due_jobs()
    assert ran == []
    run_due_jobs()
    assert ran == [1]
    db.expire_all()
    set_options(recipe_search=False)
    ran.clear()
    from datetime import UTC, datetime

    run_due_jobs(datetime.now(UTC) + timedelta(days=30))
    assert ran == []


def test_the_automatic_plan_finds_recipes_within_the_budget(admin_client, db, web, fake_openai):  # noqa: F811
    only_real_recipes(db)
    add_offer(db, "netto", "Kyllingebryst 600 g", 45)
    web.serve(URL)
    fake_openai([found(URL)])
    job_auto_plan(db)
    db.commit()
    plan = db.scalar(select(MealPlan))
    assert plan.ai_used is True
    assert plan.meals[0].title == "Kylling i karry"


def test_the_automatic_plan_gives_up_quietly_with_nothing_to_plan_from(admin_client, db):
    only_real_recipes(db)
    job_auto_plan(db)
    db.commit()
    assert db.scalar(select(MealPlan)) is None


def test_what_a_deleted_plan_left_in_mealie_is_known_as_madplans(admin_client, db, mealie):
    plan = planned_and_accepted(admin_client, db)
    send(admin_client, plan["id"])
    run_worker(db)
    mealie.entries[999] = {"id": 999, "date": plan["meals"][0]["date"], "entryType": "lunch", "title": "Rester", "text": "", "recipeId": None, "recipe": None}
    admin_client.delete(f"/api/v1/plans/{plan['id']}")
    entries = admin_client.get("/api/v1/calendar", params={"start": plan["meals"][0]["date"], "end": plan["meals"][0]["date"]}).json()["mealie"]
    assert {e["title"]: e["from_madplan"] for e in entries} == {plan["meals"][0]["title"]: True, "Rester": False}


# Sending from Home Assistant, and being reminded to


def ha_headers(admin_client) -> dict:
    token = admin_client.post("/api/v1/me/tokens", json={"name": "ha"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_home_assistant_can_press_the_send_button(admin_client, db, mealie, client):
    only_real_recipes(db)
    web_recipe(db)
    mealie.imports[URL] = good_remote()
    plan = planned_and_accepted(admin_client, db)
    headers = ha_headers(admin_client)
    summary = client.get("/api/v1/ha/summary", headers=headers).json()
    assert [m["id"] for m in summary["mealie_unsent"]] == [plan["meals"][0]["id"]]
    answer = client.post("/api/v1/ha/send-to-mealie", headers=headers)
    assert answer.json() == {"queued": 1}
    run_worker(db)
    assert len(mealie.dishes) == 1
    assert client.get("/api/v1/ha/summary", headers=headers).json()["mealie_unsent"] == []


def test_home_assistant_is_told_when_sending_is_switched_off(admin_client, db, client):
    planned_and_accepted(admin_client, db)
    answer = client.post("/api/v1/ha/send-to-mealie", headers=ha_headers(admin_client))
    assert (answer.status_code, answer.json()["detail"]) == (409, "mealie_not_connected")


def test_undecided_meals_are_not_sent_from_home_assistant_either(admin_client, db, mealie, client):
    only_real_recipes(db)
    web_recipe(db)
    plan_of(admin_client)
    assert client.post("/api/v1/ha/send-to-mealie", headers=ha_headers(admin_client)).json() == {"queued": 0}


def test_a_reminder_comes_the_day_before_the_first_unsent_meal(admin_client, db, mealie):
    from app.models import Notification
    from app.services.mealie_sync import remind_unsent
    from app.services.notifications import format_message, format_title

    only_real_recipes(db)
    web_recipe(db)
    make_user(db, "mor")
    tomorrow = local_today() + timedelta(days=1)
    plan = plan_of(admin_client, mode="range", start=tomorrow.isoformat(), end=tomorrow.isoformat())
    accept(admin_client, plan["meals"][0]["id"])
    # Two days before, it is too early to nag.
    assert remind_unsent(db, today=tomorrow - timedelta(days=2)) == 0
    assert remind_unsent(db, today=local_today()) == 2
    assert remind_unsent(db, today=local_today()) == 0
    db.commit()
    note = db.scalars(select(Notification).where(Notification.kind == "mealie_unsent")).first()
    assert format_title("mealie_unsent", note.data, "da") == "1 godkendte måltider er ikke sendt til Mealie"
    assert "Gem og Send til Mealie" in format_message("mealie_unsent", note.data, "da")
    # Nothing went to Mealie: the reminder only reminds.
    run_worker(db)
    # Reading Mealie's planner when a plan is made is fine. Nothing may be written.
    assert [c for c in mealie.calls if not c.startswith("GET ")] == []


def test_no_reminder_once_it_is_sent(admin_client, db, mealie):
    from app.services.mealie_sync import remind_unsent

    only_real_recipes(db)
    web_recipe(db)
    mealie.imports[URL] = good_remote()
    plan = planned_and_accepted(admin_client, db)
    send(admin_client, plan["id"])
    run_worker(db)
    assert remind_unsent(db) == 0
