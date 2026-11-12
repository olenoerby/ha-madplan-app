"""The "New meal plan" dialog: which days it covers, and what Mealie already has on them."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from app.core.db import local_today
from app.services import app_settings

from .conftest import set_options
from .test_new_recipes import FakeMealie, only_real_recipes, plan_of, web_recipe


@pytest.fixture
def mealie(monkeypatch):
    fake = FakeMealie()
    real = app_settings.MealieClient
    monkeypatch.setattr(app_settings, "MealieClient", lambda url, token: real(url, token, transport=httpx.MockTransport(fake.handler)))
    set_options(mealie_url="http://mealie:9000", mealie_token="t")
    return fake


def preview(client, days=3, **kwargs) -> dict:
    response = client.post("/api/v1/plans/preview", json={"mode": "days_ahead", "days_ahead": days, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def in_mealie(fake, day, meal_type="dinner", title="Lasagne", recipe=True) -> int:
    fake.next_entry += 1
    fake.entries[fake.next_entry] = {
        "id": fake.next_entry,
        "date": day.isoformat(),
        "entryType": meal_type,
        "title": "" if recipe else title,
        "text": "",
        "recipeId": f"r-{fake.next_entry}" if recipe else None,
        "recipe": {"slug": title.lower(), "name": title} if recipe else None,
    }
    return fake.next_entry


def test_the_dialog_shows_the_days_and_nothing_about_mealie_without_it(admin_client):
    result = preview(admin_client, days=3)
    today = local_today()
    assert [d["date"] for d in result["days"]] == [(today + timedelta(days=i)).isoformat() for i in range(3)]
    assert result["start"] == today.isoformat() and result["end"] == (today + timedelta(days=2)).isoformat()
    assert result["mealie"] == {"connected": False, "entries": []}


def test_the_dialog_shows_dishes_and_notes_in_mealie(admin_client, mealie):
    today = local_today()
    in_mealie(mealie, today, title="Lasagne")
    in_mealie(mealie, today + timedelta(days=1), meal_type="lunch", title="Spiser ude", recipe=False)
    in_mealie(mealie, today + timedelta(days=10), title="Too far ahead")
    result = preview(admin_client, days=3)
    assert result["mealie"]["connected"] is True
    seen = {(e["date"], e["meal_type"], e["title"], e["kind"]) for e in result["mealie"]["entries"]}
    assert seen == {(today.isoformat(), "dinner", "Lasagne", "recipe"), ((today + timedelta(days=1)).isoformat(), "lunch", "Spiser ude", "note")}
    # Reading is all the dialog does.
    assert [c for c in mealie.calls if not c.startswith("GET ")] == []


def test_a_dish_already_in_mealie_keeps_its_place(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    today = local_today()
    in_mealie(mealie, today, title="Lasagne")
    plan = plan_of(admin_client, days=2)
    first = [m for m in plan["meals"] if m["date"] == today.isoformat() and m["meal_type"] == "dinner"]
    second = [m for m in plan["meals"] if m["date"] == (today + timedelta(days=1)).isoformat()]
    # Mealie's dish is on the plan, accepted, and MadPlan suggested nothing else for that dinner.
    assert [(m["title"], m["status"]) for m in first] == [("Lasagne", "accepted")]
    assert [m["title"] for m in second] == ["Kylling i karry"]
    assert not any(e.get("recipeId") is None and "Kylling" in str(e.get("recipe")) for e in mealie.entries.values())


def test_a_note_can_have_a_dish_beside_it_or_skip_the_day(admin_client, db, mealie):
    only_real_recipes(db)
    web_recipe(db)
    web_recipe(db, title="Laks i ovn", url="https://madsiden.dk/laks")
    today = local_today()
    in_mealie(mealie, today, title="Rester fra i går", recipe=False)
    in_mealie(mealie, today + timedelta(days=1), title="Spiser ude", recipe=False)
    plan = plan_of(admin_client, days=2, skip_dates=[(today + timedelta(days=1)).isoformat()])
    meals = {(m["date"], m["title"]) for m in plan["meals"] if m["status"] != "rejected"}
    # A dish beside the first note, and the note stays a note in Mealie.
    assert len([m for m in meals if m[0] == today.isoformat()]) == 1
    assert (today.isoformat(), "Rester fra i går") not in meals
    # The skipped day has nothing, and "eating out" did not become a meal.
    assert not [m for m in meals if m[0] == (today + timedelta(days=1)).isoformat()]
    assert next(d for d in plan["days"] if d["date"] == (today + timedelta(days=1)).isoformat())["skipped"] is True
    # Still there in Mealie.
    assert {e["title"] for e in mealie.entries.values()} >= {"Rester fra i går", "Spiser ude"}


def test_a_note_in_mealie_can_be_written_from_the_dialog(admin_client, mealie):
    day = local_today() + timedelta(days=2)
    response = admin_client.post("/api/v1/mealie/notes", json={"date": day.isoformat(), "meal_type": "dinner", "title": "Spiser ude"})
    assert response.status_code == 201, response.text
    entry = mealie.entries[int(response.json()["id"])]
    assert (entry["date"], entry["entryType"], entry["title"], entry.get("recipeId")) == (day.isoformat(), "dinner", "Spiser ude", None)
    # It is the person's own note, not marked as MadPlan's.
    assert "MadPlan" not in (entry.get("text") or "")
    seen = preview(admin_client, days=3)["mealie"]["entries"]
    assert [(e["title"], e["kind"]) for e in seen] == [("Spiser ude", "note")]
    # Not in the past.
    past = (local_today() - timedelta(days=1)).isoformat()
    assert admin_client.post("/api/v1/mealie/notes", json={"date": past, "title": "x"}).status_code == 422


def test_no_notes_in_mealie_without_mealie(admin_client):
    response = admin_client.post("/api/v1/mealie/notes", json={"date": local_today().isoformat(), "title": "Spiser ude"})
    assert response.status_code == 409
    assert response.json()["detail"] == "mealie_not_configured"
