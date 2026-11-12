"""Mealie has the last word, when it is set up: changes there come back into MadPlan."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from app.core.db import local_today
from app.models import PlannedMeal
from app.services import app_settings
from app.worker import job_mealie_pull

from .conftest import household_of_admin, set_options
from .test_new_recipes import URL, FakeMealie, good_remote, only_real_recipes, planned_and_accepted, run_worker, send, web_recipe


class MealieWithAList(FakeMealie):
    """Mealie, with the shopping list MadPlan uses, as people tick it off in Mealie."""

    def __init__(self) -> None:
        super().__init__()
        self.list_items: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/households/shopping/lists/L1" and request.method == "GET":
            return httpx.Response(200, json={"id": "L1", "name": "MadPlan", "listItems": self.list_items})
        if path == "/api/households/mealplans" and request.method == "GET":
            return httpx.Response(200, json={"items": list(self.entries.values())})
        return super().handler(request)


@pytest.fixture
def mealie(monkeypatch, db):
    fake = MealieWithAList()
    real = app_settings.MealieClient
    monkeypatch.setattr(app_settings, "MealieClient", lambda url, token: real(url, token, transport=httpx.MockTransport(fake.handler)))
    set_options(mealie_url="http://mealie:9000", mealie_public_url="https://mad.example.dk/", mealie_token="t")
    return fake


def sent_plan(client, db, mealie, days=3):
    only_real_recipes(db)
    web_recipe(db)
    mealie.imports[URL] = good_remote()
    plan = planned_and_accepted(client, db, days=days)
    send(client, plan["id"])
    run_worker(db)
    return client.get(f"/api/v1/plans/{plan['id']}").json()


def pull(db):
    db.expire_all()
    job_mealie_pull(db)
    db.commit()
    db.expire_all()


def test_a_dish_moved_in_mealie_moves_here(admin_client, db, mealie):
    plan = sent_plan(admin_client, db, mealie)
    meal = plan["meals"][0]
    assert meal["sync_status"] == "synced"
    row = db.get(PlannedMeal, meal["id"])
    entry = mealie.entries[int(row.mealie_entry_id)]
    new_day = (local_today() + timedelta(days=2)).isoformat()
    entry["date"] = new_day
    pull(db)
    after = db.get(PlannedMeal, meal["id"])
    assert after.date.isoformat() == new_day
    # And it is not sent back as a change of its own.
    assert admin_client.get(f"/api/v1/plans/{plan['id']}").json()["mealie"]["to_send"] == 0


def test_a_dish_removed_in_mealie_is_turned_down_here(admin_client, db, mealie):
    plan = sent_plan(admin_client, db, mealie, days=1)
    row = db.get(PlannedMeal, plan["meals"][0]["id"])
    row.bought = "full"
    db.commit()
    del mealie.entries[int(row.mealie_entry_id)]
    pull(db)
    after = db.get(PlannedMeal, plan["meals"][0]["id"])
    # Mealie wins, even over food that is bought.
    assert (after.status, after.mealie_entry_id) == ("rejected", None)
    assert admin_client.get(f"/api/v1/plans/{plan['id']}").json()["mealie"]["to_send"] == 0


def test_another_meal_or_recipe_in_mealie_changes_it_here(admin_client, db, mealie):
    plan = sent_plan(admin_client, db, mealie, days=1)
    row = db.get(PlannedMeal, plan["meals"][0]["id"])
    entry = mealie.entries[int(row.mealie_entry_id)]
    entry["entryType"] = "lunch"
    entry["recipeId"] = "id-ukendt"
    entry["recipe"] = {"slug": "pandekager", "name": "Pandekager"}
    pull(db)
    after = db.get(PlannedMeal, plan["meals"][0]["id"])
    assert (after.meal_type, after.title, after.recipe_id) == ("lunch", "Pandekager", None)


def test_a_dish_added_in_mealie_comes_onto_the_plan(admin_client, db, mealie):
    plan = sent_plan(admin_client, db, mealie, days=2)
    day = plan["meals"][0]["date"]
    mealie.entries[500] = {"id": 500, "date": day, "entryType": "lunch", "title": "Rugbrød med æg", "text": "", "recipeId": None, "recipe": None}
    pull(db)
    meals = admin_client.get(f"/api/v1/plans/{plan['id']}").json()["meals"]
    added = next(m for m in meals if m["title"] == "Rugbrød med æg")
    assert (added["status"], added["meal_type"], added["date"]) == ("accepted", "lunch", day)
    assert added["reasons"][0]["code"] == "from_mealie"
    # Kept track of, and never sent back as new.
    pull(db)
    assert sum(1 for m in admin_client.get(f"/api/v1/plans/{plan['id']}").json()["meals"] if m["title"] == "Rugbrød med æg") == 1
    assert admin_client.get(f"/api/v1/plans/{plan['id']}").json()["mealie"]["to_send"] == 0
    assert admin_client.get(f"/api/v1/plans/{plan['id']}/mealie-entries").json() == []


def test_what_madplan_put_in_mealie_is_not_taken_for_a_new_dish(admin_client, db, mealie):
    plan = sent_plan(admin_client, db, mealie, days=1)
    count = len(plan["meals"])
    mealie.entries[600] = {"id": 600, "date": plan["meals"][0]["date"], "entryType": "dinner", "title": "Gammel", "text": "Fra MadPlan", "recipeId": None, "recipe": None}
    pull(db)
    assert len(admin_client.get(f"/api/v1/plans/{plan['id']}").json()["meals"]) == count


def test_ticks_in_mealie_come_back_here(admin_client, db, mealie):
    plan = sent_plan(admin_client, db, mealie, days=1)
    items = plan["shopping_list"]["items"]
    target = next(i for i in items if not i["covered_by_pantry"])
    mealie.list_items = [{"food": {"name": target["name"]}, "note": "", "checked": True}]
    pull(db)
    after = admin_client.get(f"/api/v1/shopping-lists/{plan['shopping_list']['id']}").json()["items"]
    assert next(i for i in after if i["id"] == target["id"])["checked"] is True
    mealie.list_items[0]["checked"] = False
    pull(db)
    after = admin_client.get(f"/api/v1/shopping-lists/{plan['shopping_list']['id']}").json()["items"]
    assert next(i for i in after if i["id"] == target["id"])["checked"] is False


def test_nothing_is_read_from_mealie_when_it_is_not_set_up(admin_client, db):
    assert app_settings.mealie_config(db, household_of_admin(db).id) is None
    job_mealie_pull(db)
