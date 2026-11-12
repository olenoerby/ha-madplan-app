"""Food coming in and going out, running low, buy signals, automatic plans, and leaving.

These follow what a household actually does over a few weeks, since that is where the pantry
went wrong: everything ever bought went on counting as being at home.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.core.db import local_hour, local_today, utcnow
from app.models import (
    ChatMessage,
    Household,
    MealPlan,
    MealVote,
    Notification,
    PantryItem,
    PlannedMeal,
    Recipe,
    ShoppingItem,
    ShoppingList,
    User,
)
from app.services.alerts import run_buy_signals
from app.services.app_settings import get_schedule, save_schedule
from app.services.catalog import keeps_days
from app.services.favorites import learn_taste
from app.services.notifications import format_message, page_link
from app.services.pantry import consume_past_meals, forget_old_estimates
from app.services.planner import notify_plan_ready, refresh_open_plans
from app.services.restock import buying_rhythm, run_restock_check
from app.worker import job_auto_plan, job_cleanup

from .conftest import add_offer, household_of_admin, login_as, make_user, set_options
from .test_ai import answer as ai_answer
from .test_ai import fake_openai, text_message  # noqa: F401
from .test_household_features import HISTORY, add_history
from .test_planning import make_plan, set_household


def pantry_row(db, name, household=None, **kwargs) -> PantryItem:
    item = PantryItem(household_id=(household or household_of_admin(db)).id, name=name, **kwargs)
    db.add(item)
    db.commit()
    return item


def pantry_amount(db, name) -> float | None:
    db.expire_all()
    item = db.scalar(select(PantryItem).where(PantryItem.name == name))
    return item.quantity if item else None


def own_recipe(db, title="Testret", ingredients=None, servings=2, meal_types=("dinner",)) -> Recipe:
    recipe = Recipe(
        household_id=household_of_admin(db).id,
        source="mealie",
        title=title,
        meal_types=list(meal_types),
        servings=servings,
        ingredients=ingredients or [{"name": "hakket oksekød", "quantity": 400, "unit": "g"}],
        steps=["Steg."],
        tags=[],
        # The household's own, which here means that it is in Mealie.
        mealie_slug=f"testret-{title.lower()}",
        mealie_id=f"id-{title.lower()}",
    )
    db.add(recipe)
    db.commit()
    return recipe


def planned_meal(db, recipe, days_from_today, status="accepted", list_completed=False) -> PlannedMeal:
    """One meal in a plan of its own, without going through the planner."""
    day = local_today() + timedelta(days=days_from_today)
    plan = MealPlan(household_id=household_of_admin(db).id, start_date=day, end_date=day, meal_types=["dinner"])
    db.add(plan)
    db.flush()
    meal = PlannedMeal(plan_id=plan.id, date=day, meal_type="dinner", recipe_id=recipe.id, title=recipe.title, adults=2, children=0, status=status)
    db.add(meal)
    if list_completed:
        db.add(ShoppingList(household_id=plan.household_id, plan_id=plan.id, name="x", completed_at=utcnow()))
    db.commit()
    return meal


def one_day_plan_of(client, db, recipe):
    """Makes the planner pick this one recipe, by being the only dinner anyone likes."""
    set_household(db, likes=[recipe.title.lower()], budget_weekly=None)
    plan = make_plan(client, mode="days_ahead", days_ahead=1)
    meal = plan["meals"][0]
    if meal["recipe_id"] != recipe.id:
        plan = client.post(f"/api/v1/meals/{meal['id']}/replace", json={"recipe_id": recipe.id}).json()
    return plan


def buy_everything(client, plan):
    shopping = plan["shopping_list"]
    for item in shopping["items"]:
        if not item["covered_by_pantry"]:
            assert client.patch(f"/api/v1/shopping-items/{item['id']}", json={"checked": True}).status_code == 200
    response = client.post(f"/api/v1/shopping-lists/{shopping['id']}/complete", json={})
    assert response.status_code == 200, response.text


def to_buy(plan):
    return {i["name"]: i for i in plan["shopping_list"]["items"] if not i["covered_by_pantry"]}


# What comes in


def test_whole_packs_come_home_and_not_just_what_the_recipe_used(admin_client, db):
    add_offer(db, "netto", "Hakket oksekød 500 g", 30.0, pack_quantity=500, pack_unit="g", unit_price=60, unit_basis="kg")
    recipe = own_recipe(db)
    plan = one_day_plan_of(admin_client, db, recipe)
    buy_everything(admin_client, plan)
    assert pantry_amount(db, "hakket oksekød") == 500


def test_bought_fresh_food_gets_a_date_marked_as_an_estimate(admin_client, db):
    recipe = own_recipe(db)
    plan = one_day_plan_of(admin_client, db, recipe)
    buy_everything(admin_client, plan)
    item = next(i for i in admin_client.get("/api/v1/pantry").json() if i["name"] == "hakket oksekød")
    assert item["expires_estimated"] is True
    assert item["expires_on"] == (local_today() + timedelta(days=keeps_days("hakket oksekød"))).isoformat()


def test_the_estimate_can_be_switched_off(admin_client, db):
    recipe = own_recipe(db)
    plan = one_day_plan_of(admin_client, db, recipe)
    set_household(db, estimate_expiry=False)
    buy_everything(admin_client, plan)
    item = next(i for i in admin_client.get("/api/v1/pantry").json() if i["name"] == "hakket oksekød")
    assert (item["expires_on"], item["expires_estimated"]) == (None, False)


def test_frozen_food_gets_no_fridge_date():
    assert keeps_days("bær") is not None
    assert keeps_days("frosne bær") is None
    assert keeps_days("en ting vi ikke kender") is None


def test_changing_the_date_makes_it_yours_and_leaving_it_keeps_the_estimate(admin_client, db):
    guess = local_today() + timedelta(days=2)
    item = pantry_row(db, "mælk", quantity=1000, unit="ml", expires_on=guess, expires_estimated=True)
    body = {"name": "mælk", "quantity": 500, "unit": "ml", "expires_on": guess.isoformat()}
    assert admin_client.put(f"/api/v1/pantry/{item.id}", json=body).json()["expires_estimated"] is True
    body["expires_on"] = (guess + timedelta(days=5)).isoformat()
    assert admin_client.put(f"/api/v1/pantry/{item.id}", json=body).json()["expires_estimated"] is False


def test_a_new_pack_tops_up_what_is_there_in_its_own_unit(admin_client, db):
    pantry_row(db, "Hakket Oksekød", quantity=1, unit="kg")
    recipe = own_recipe(db, ingredients=[{"name": "hakket oksekød", "quantity": 2000, "unit": "g"}])
    plan = one_day_plan_of(admin_client, db, recipe)
    buy_everything(admin_client, plan)
    db.expire_all()
    rows = db.scalars(select(PantryItem)).all()
    assert len(rows) == 1
    assert rows[0].quantity == pytest.approx(2.0, abs=0.51)
    assert rows[0].quantity > 1


def test_something_typed_in_without_an_amount_counts_as_a_pack(admin_client, db):
    """Otherwise it could never run out, and milk would be at home for ever after."""
    shopping = ShoppingList(household_id=household_of_admin(db).id, name="x")
    db.add(shopping)
    db.flush()
    item = ShoppingItem(list_id=shopping.id, name="mælk", manual=True, checked=True)
    db.add(item)
    db.commit()
    assert admin_client.post(f"/api/v1/shopping-lists/{shopping.id}/complete", json={}).status_code == 200
    assert pantry_amount(db, "mælk") == 1000


# What goes out


def test_next_week_buys_again_what_last_week_ate(admin_client, db):
    """The bug itself: last week's mince must not be counted as being in the fridge."""
    add_offer(db, "netto", "Hakket oksekød 400 g", 30.0, pack_quantity=400, pack_unit="g", unit_price=75, unit_basis="kg")
    recipe = own_recipe(db)
    plan = one_day_plan_of(admin_client, db, recipe)
    admin_client.post(f"/api/v1/plans/{plan['id']}/accept-all")
    buy_everything(admin_client, plan)
    assert pantry_amount(db, "hakket oksekød") == 400

    # While it is in the fridge, the next list does not need to buy it.
    second = planned_meal(db, recipe, 1)
    from app.services.shopping import build_shopping_list

    shopping = build_shopping_list(db, second.plan)
    assert not [i for i in shopping.items if i.name == "hakket oksekød" and not i.covered_by_pantry]

    assert consume_past_meals(db, local_today() + timedelta(days=1)) == 1
    db.commit()
    assert pantry_amount(db, "hakket oksekød") is None

    third = planned_meal(db, recipe, 2)
    shopping = build_shopping_list(db, third.plan)
    assert [i for i in shopping.items if i.name == "hakket oksekød" and not i.covered_by_pantry]


def test_a_meal_only_uses_its_own_share(admin_client, db):
    pantry_row(db, "hakket oksekød", quantity=1000, unit="g")
    planned_meal(db, own_recipe(db), -1)
    assert consume_past_meals(db) == 1
    db.commit()
    assert pantry_amount(db, "hakket oksekød") == 600


def test_guests_eat_too(admin_client, db):
    pantry_row(db, "hakket oksekød", quantity=1000, unit="g")
    meal = planned_meal(db, own_recipe(db), -1)
    meal.adults = 4
    db.commit()
    consume_past_meals(db)
    db.commit()
    assert pantry_amount(db, "hakket oksekød") == 200


def test_it_only_counts_once(admin_client, db):
    pantry_row(db, "hakket oksekød", quantity=1000, unit="g")
    planned_meal(db, own_recipe(db), -1)
    assert consume_past_meals(db) == 1
    assert consume_past_meals(db) == 0
    db.commit()
    assert pantry_amount(db, "hakket oksekød") == 600


@pytest.mark.parametrize(
    ("status", "list_completed", "eaten"),
    [("accepted", False, True), ("pending", True, True), ("pending", False, False), ("rejected", True, False)],
)
def test_what_counts_as_eaten(admin_client, db, status, list_completed, eaten):
    pantry_row(db, "hakket oksekød", quantity=1000, unit="g")
    planned_meal(db, own_recipe(db), -1, status, list_completed)
    consume_past_meals(db)
    db.commit()
    assert pantry_amount(db, "hakket oksekød") == (600 if eaten else 1000)


def test_today_is_not_over_yet(admin_client, db):
    pantry_row(db, "hakket oksekød", quantity=1000, unit="g")
    planned_meal(db, own_recipe(db), 0)
    assert consume_past_meals(db) == 0


def test_tracking_can_be_switched_off_for_a_household(admin_client, db):
    pantry_row(db, "hakket oksekød", quantity=1000, unit="g")
    planned_meal(db, own_recipe(db), -1)
    set_household(db, track_consumption=False)
    assert consume_past_meals(db) == 0


def test_a_different_unit_is_converted(admin_client, db):
    pantry_row(db, "hakket oksekød", quantity=1, unit="kg")
    planned_meal(db, own_recipe(db), -1)
    consume_past_meals(db)
    db.commit()
    assert pantry_amount(db, "hakket oksekød") == pytest.approx(0.6)


def test_pieces_and_weight_can_be_compared_when_the_weight_of_one_is_known(admin_client, db):
    pantry_row(db, "løg", quantity=600, unit="g")
    planned_meal(db, own_recipe(db, ingredients=[{"name": "løg", "quantity": 2, "unit": "stk"}]), -1)
    consume_past_meals(db)
    db.commit()
    assert pantry_amount(db, "løg") == 360


def test_an_unknown_amount_stays_as_it_is(admin_client, db):
    pantry_row(db, "hakket oksekød")
    planned_meal(db, own_recipe(db), -1)
    assert consume_past_meals(db) == 1
    db.commit()
    assert db.scalar(select(PantryItem)).quantity is None


def test_what_goes_off_first_gets_used_first(admin_client, db):
    soon = pantry_row(db, "hakket oksekød", quantity=300, unit="g", expires_on=local_today() + timedelta(days=1))
    later = pantry_row(db, "Hakket oksekød", quantity=300, unit="g", expires_on=local_today() + timedelta(days=9))
    soon_id, later_id = soon.id, later.id
    planned_meal(db, own_recipe(db), -1)
    consume_past_meals(db)
    db.commit()
    db.expire_all()
    assert db.get(PantryItem, soon_id) is None
    assert db.get(PantryItem, later_id).quantity == 200


def test_an_empty_row_goes_away_unless_someone_wants_to_be_reminded(admin_client, db):
    pantry_row(db, "hakket oksekød", quantity=400, unit="g")
    pantry_row(db, "kaffe", quantity=400, unit="g", min_quantity=100)
    planned_meal(db, own_recipe(db, ingredients=[{"name": "hakket oksekød", "quantity": 400, "unit": "g"}, {"name": "kaffe", "quantity": 400, "unit": "g"}]), -1)
    consume_past_meals(db)
    db.commit()
    db.expire_all()
    assert pantry_amount(db, "hakket oksekød") is None
    assert pantry_amount(db, "kaffe") == 0


# Saying what really happened


def test_a_meal_that_was_not_made_puts_the_food_back(admin_client, db):
    pantry_row(db, "hakket oksekød", quantity=400, unit="g", location="fryser")
    meal = planned_meal(db, own_recipe(db), -1)
    consume_past_meals(db)
    db.commit()
    assert pantry_amount(db, "hakket oksekød") is None

    plan = admin_client.post(f"/api/v1/meals/{meal.id}/cooked", json={"cooked": False}).json()
    assert plan["meals"][0]["cooked"] is False
    assert plan["meals"][0]["consumed"] is False
    db.expire_all()
    row = db.scalar(select(PantryItem).where(PantryItem.name == "hakket oksekød"))
    assert (row.quantity, row.location) == (400, "fryser")
    # The job leaves it alone from then on.
    assert consume_past_meals(db) == 0


def test_saying_it_was_made_takes_the_food_out_right_away(admin_client, db):
    pantry_row(db, "hakket oksekød", quantity=1000, unit="g")
    meal = planned_meal(db, own_recipe(db), 0, status="pending")
    plan = admin_client.post(f"/api/v1/meals/{meal.id}/cooked", json={"cooked": True}).json()
    assert plan["meals"][0]["consumed"] is True
    assert pantry_amount(db, "hakket oksekød") == 600
    # And not a second time when the day has passed.
    assert consume_past_meals(db, local_today() + timedelta(days=1)) == 0


def test_saying_so_after_the_worker_got_there_first_changes_nothing(admin_client, db):
    """Yesterday's dinner has already left the pantry when someone presses the button."""
    pantry_row(db, "hakket oksekød", quantity=1000, unit="g")
    meal = planned_meal(db, own_recipe(db), -1)
    consume_past_meals(db)
    db.commit()
    assert pantry_amount(db, "hakket oksekød") == 600
    assert admin_client.post(f"/api/v1/meals/{meal.id}/cooked", json={"cooked": True}).status_code == 200
    assert admin_client.post(f"/api/v1/meals/{meal.id}/cooked", json={"cooked": True}).status_code == 200
    assert pantry_amount(db, "hakket oksekød") == 600


def test_tomorrows_dinner_cannot_have_been_made(admin_client, db):
    meal = planned_meal(db, own_recipe(db), 1)
    assert admin_client.post(f"/api/v1/meals/{meal.id}/cooked", json={"cooked": True}).status_code == 409


def test_rejecting_a_meal_afterwards_puts_the_food_back(admin_client, db):
    pantry_row(db, "hakket oksekød", quantity=1000, unit="g")
    meal = planned_meal(db, own_recipe(db), -1)
    consume_past_meals(db)
    db.commit()
    assert admin_client.post(f"/api/v1/meals/{meal.id}/vote", json={"vote": "no"}).status_code == 200
    assert pantry_amount(db, "hakket oksekød") == 1000


def test_a_trip_registered_late_still_loses_what_was_eaten_since(admin_client, db):
    recipe = own_recipe(db)
    plan = one_day_plan_of(admin_client, db, recipe)
    admin_client.post(f"/api/v1/plans/{plan['id']}/accept-all")
    meal = db.get(PlannedMeal, plan["meals"][0]["id"])
    meal.date = local_today() - timedelta(days=1)
    db.commit()
    # The job finds nothing to take, as nothing was registered as bought yet.
    assert consume_past_meals(db) == 1
    db.commit()
    db.expire_all()
    buy_everything(admin_client, plan)
    db.expire_all()
    assert db.get(PlannedMeal, plan["meals"][0]["id"]).consumed
    assert (pantry_amount(db, "hakket oksekød") or 0) < 400


def test_a_meal_not_made_is_not_in_the_diet_report_or_blocking_a_repeat(admin_client, db):
    meal = planned_meal(db, own_recipe(db, title="Særlig gryde"), -1)
    assert admin_client.get("/api/v1/diet/report").json()["meals"] == 1
    admin_client.post(f"/api/v1/meals/{meal.id}/cooked", json={"cooked": False})
    assert admin_client.get("/api/v1/diet/report").json()["meals"] == 0
    from app.services.planner import recently_served

    assert recently_served(db, household_of_admin(db).id, local_today(), 21) == {}


def test_someone_elses_meal_cannot_be_touched(admin_client, db):
    other = Household(name="Naboen", settings={})
    db.add(other)
    db.flush()
    plan = MealPlan(household_id=other.id, start_date=local_today(), end_date=local_today(), meal_types=["dinner"])
    db.add(plan)
    db.flush()
    meal = PlannedMeal(plan_id=plan.id, date=local_today(), meal_type="dinner", title="Hemmelig ret")
    db.add(meal)
    db.commit()
    assert admin_client.post(f"/api/v1/meals/{meal.id}/cooked", json={"cooked": True}).status_code == 404


# What can be counted on


def test_food_past_its_date_is_bought_anew(admin_client, db):
    pantry_row(db, "hakket oksekød", quantity=2000, unit="g", expires_on=local_today() - timedelta(days=1))
    plan = one_day_plan_of(admin_client, db, own_recipe(db))
    assert "hakket oksekød" in to_buy(plan)


def test_food_that_goes_off_before_the_plan_starts_is_bought_anew(admin_client, db):
    pantry_row(db, "hakket oksekød", quantity=2000, unit="g", expires_on=local_today() + timedelta(days=1))
    recipe = own_recipe(db)
    meal = planned_meal(db, recipe, 5)
    from app.services.shopping import build_shopping_list

    shopping = build_shopping_list(db, meal.plan)
    assert [i for i in shopping.items if i.name == "hakket oksekød" and not i.covered_by_pantry]


def test_an_empty_row_counts_for_nothing(admin_client, db):
    pantry_row(db, "hakket oksekød", quantity=0, unit="g", min_quantity=100)
    plan = one_day_plan_of(admin_client, db, own_recipe(db))
    assert "hakket oksekød" in to_buy(plan)
    assert not [r for r in plan["meals"][0]["reasons"] if r["code"] == "uses_pantry"]


def test_a_leftover_does_not_make_a_dish_free(admin_client, db):
    add_offer(db, "netto", "Hakket oksekød 500 g", 50.0, pack_quantity=500, pack_unit="g", unit_price=100, unit_basis="kg")
    plan_without = one_day_plan_of(admin_client, db, own_recipe(db))
    full_price = plan_without["meals"][0]["estimated_cost"]
    assert full_price > 0
    admin_client.delete(f"/api/v1/plans/{plan_without['id']}")

    pantry_row(db, "hakket oksekød", quantity=30, unit="g")
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    meal = plan["meals"][0]
    assert meal["estimated_cost"] > 0
    # What is left over is not enough to count as using the pantry.
    assert not [r for r in meal["reasons"] if r["code"] == "uses_pantry"]
    assert to_buy(plan)["hakket oksekød"]["quantity"] == 370


def test_a_real_amount_in_the_pantry_covers_its_share(admin_client, db):
    pantry_row(db, "hakket oksekød", quantity=300, unit="g")
    plan = one_day_plan_of(admin_client, db, own_recipe(db))
    assert to_buy(plan)["hakket oksekød"]["quantity"] == 100
    assert [r for r in plan["meals"][0]["reasons"] if r["code"] == "uses_pantry"]


def test_estimated_dates_are_forgotten_long_after_but_real_ones_are_not(admin_client, db):
    old = local_today() - timedelta(days=30)
    guess = pantry_row(db, "gammel mælk", expires_on=old, expires_estimated=True)
    real = pantry_row(db, "gammel ost", expires_on=old)
    wanted = pantry_row(db, "gammel kaffe", expires_on=old, expires_estimated=True, min_quantity=1)
    recent = pantry_row(db, "ny mælk", expires_on=local_today() - timedelta(days=2), expires_estimated=True)
    ids = (guess.id, real.id, wanted.id, recent.id)
    assert forget_old_estimates(db) == 1
    db.commit()
    db.expire_all()
    assert [db.get(PantryItem, i) is not None for i in ids] == [False, True, True, True]


# The list


def test_a_finished_list_stays_the_record_of_the_trip(admin_client, db):
    add_offer(db, "netto", "Hakket oksekød 500 g", 30.0, pack_quantity=500, pack_unit="g", unit_price=60, unit_basis="kg")
    plan = one_day_plan_of(admin_client, db, own_recipe(db))
    total = plan["shopping_list"]["summary"]["total"]
    buy_everything(admin_client, plan)
    refreshed = admin_client.post(f"/api/v1/plans/{plan['id']}/refresh-list").json()
    assert refreshed["summary"]["total"] == total
    assert to_buy({"shopping_list": refreshed})


def test_a_list_redone_midweek_does_not_buy_for_days_gone_by(admin_client, db):
    recipe = own_recipe(db)
    plan = make_plan(admin_client, mode="range", start=(local_today() - timedelta(days=1)).isoformat(), end=(local_today() + timedelta(days=1)).isoformat())
    meals = {m["date"]: m for m in plan["meals"]}
    yesterday = (local_today() - timedelta(days=1)).isoformat()
    admin_client.post(f"/api/v1/meals/{meals[yesterday]['id']}/replace", json={"recipe_id": recipe.id})
    tomorrow = (local_today() + timedelta(days=1)).isoformat()
    other = own_recipe(db, title="Laksefad", ingredients=[{"name": "laks", "quantity": 400, "unit": "g"}])
    admin_client.post(f"/api/v1/meals/{meals[tomorrow]['id']}/replace", json={"recipe_id": other.id})
    refreshed = admin_client.post(f"/api/v1/plans/{plan['id']}/refresh-list").json()
    names = {i["name"] for i in refreshed["items"]}
    assert "laks" in names
    assert "hakket oksekød" not in names


# Running low


def test_below_the_minimum_is_suggested_with_the_best_offer(admin_client, db):
    pantry_row(db, "toiletpapir", quantity=2, unit="stk", min_quantity=4)
    add_offer(db, "netto", "Toiletpapir 12 ruller", 45.0)
    add_offer(db, "lidl", "Toiletpapir 8 ruller", 29.0)
    found = admin_client.get("/api/v1/pantry/restock").json()
    assert [s["name"] for s in found] == ["toiletpapir"]
    assert found[0]["reason"] == "below_minimum"
    assert found[0]["offer"]["price"] == 29.0


def test_enough_is_enough(admin_client, db):
    pantry_row(db, "toiletpapir", quantity=4, unit="stk", min_quantity=4)
    pantry_row(db, "kaffe", quantity=400, unit="g")
    pantry_row(db, "sæbe", min_quantity=1)
    assert admin_client.get("/api/v1/pantry/restock").json() == []


def test_food_past_its_date_counts_as_gone(admin_client, db):
    pantry_row(db, "mælk", quantity=5000, unit="ml", min_quantity=1000, expires_on=local_today() - timedelta(days=1))
    assert [s["name"] for s in admin_client.get("/api/v1/pantry/restock").json()] == ["mælk"]


def test_what_is_already_on_a_list_is_not_suggested(admin_client, db):
    pantry_row(db, "toiletpapir", quantity=0, unit="stk", min_quantity=4)
    added = admin_client.post("/api/v1/pantry/restock/add", json={"name": "Toiletpapir"})
    assert added.status_code == 201
    assert admin_client.get("/api/v1/pantry/restock").json() == []
    lists = admin_client.get("/api/v1/shopping-lists").json()
    assert lists[0]["name"] == "Løbende indkøb"


def test_not_now_comes_back_in_a_week(admin_client, db):
    pantry_row(db, "toiletpapir", quantity=0, unit="stk", min_quantity=4)
    key = admin_client.get("/api/v1/pantry/restock").json()[0]["key"]
    assert admin_client.post("/api/v1/pantry/restock/snooze", json={"key": key}).status_code == 200
    assert admin_client.get("/api/v1/pantry/restock").json() == []
    from app.services.restock import restock_suggestions

    db.expire_all()
    household = household_of_admin(db)
    assert restock_suggestions(db, household, None, local_today() + timedelta(days=6), False) == []
    assert restock_suggestions(db, household, None, local_today() + timedelta(days=7), False)


def test_any_member_can_say_not_now(admin_client, db):
    pantry_row(db, "toiletpapir", quantity=0, unit="stk", min_quantity=4)
    make_user(db, "anna")
    assert login_as("anna").post("/api/v1/pantry/restock/snooze", json={"key": "toiletpapir"}).status_code == 200


def test_what_someone_must_not_eat_is_never_suggested(admin_client, db):
    set_household(db, allergies=["nødder"])
    pantry_row(db, "peanuts", quantity=0, unit="g", min_quantity=100)
    pantry_row(db, "toiletpapir", quantity=0, unit="stk", min_quantity=4)
    assert [s["name"] for s in admin_client.get("/api/v1/pantry/restock").json()] == ["toiletpapir"]


def test_a_suggested_offer_never_contains_what_someone_must_not_eat(admin_client, db):
    set_household(db, allergies=["nødder"])
    pantry_row(db, "müsli", quantity=0, unit="g", min_quantity=100)
    add_offer(db, "netto", "Müsli med nødder", 10.0)
    add_offer(db, "lidl", "Müsli med rosiner", 25.0)
    assert admin_client.get("/api/v1/pantry/restock").json()[0]["offer"]["price"] == 25.0


def test_adding_something_on_the_allergy_list_warns(admin_client, db):
    set_household(db, allergies=["nødder"])
    assert admin_client.post("/api/v1/pantry/restock/add", json={"name": "peanuts"}).json()["allergy_warning"]


def test_other_households_stock_is_nobodys_business(admin_client, db):
    other = Household(name="Naboen", settings={})
    db.add(other)
    db.commit()
    pantry_row(db, "naboens toiletpapir", household=other, quantity=0, unit="stk", min_quantity=4)
    assert admin_client.get("/api/v1/pantry/restock").json() == []


def buy_by_hand(db, name, days_ago, household=None):
    household = household or household_of_admin(db)
    when = datetime.now(UTC) - timedelta(days=days_ago)
    shopping = ShoppingList(household_id=household.id, name="x", completed_at=when)
    db.add(shopping)
    db.flush()
    db.add(ShoppingItem(list_id=shopping.id, name=name, manual=True, checked=True))
    db.commit()


def test_a_habit_is_noticed_when_it_is_nearly_time_again(admin_client, db):
    for days_ago in (61, 40, 19):
        buy_by_hand(db, "Kaffe", days_ago)
    found = admin_client.get("/api/v1/pantry/restock").json()
    assert [(s["name"], s["reason"], s["every_days"]) for s in found] == [("Kaffe", "usually_bought", 21)]


def test_no_habit_yet_and_too_soon_say_nothing(admin_client, db):
    buy_by_hand(db, "kaffe", 40)
    buy_by_hand(db, "kaffe", 19)
    for days_ago in (61, 40, 5):
        buy_by_hand(db, "mælk", days_ago)
    assert admin_client.get("/api/v1/pantry/restock").json() == []


def test_a_habit_they_gave_up_is_left_alone(admin_client, db):
    for days_ago in (200, 179, 158):
        buy_by_hand(db, "kaffe", days_ago)
    assert admin_client.get("/api/v1/pantry/restock").json() == []


def test_what_plans_buy_is_not_a_habit(admin_client, db):
    for days_ago in (61, 40, 19):
        shopping = ShoppingList(household_id=household_of_admin(db).id, name="x", completed_at=datetime.now(UTC) - timedelta(days=days_ago))
        db.add(shopping)
        db.flush()
        db.add(ShoppingItem(list_id=shopping.id, name="hakket oksekød", manual=False, checked=True))
    db.commit()
    assert admin_client.get("/api/v1/pantry/restock").json() == []


def test_one_big_shopping_week_is_not_a_rhythm():
    today = local_today()
    assert buying_rhythm([today, today - timedelta(days=1), today - timedelta(days=2)]) is None
    assert buying_rhythm([today, today - timedelta(days=7)]) is None
    assert buying_rhythm([today, today - timedelta(days=7), today - timedelta(days=14)]) == 7


def test_someone_saying_there_is_some_left_wins_over_the_habit(admin_client, db):
    for days_ago in (61, 40, 19):
        buy_by_hand(db, "kaffe", days_ago)
    item = pantry_row(db, "kaffe", quantity=400, unit="g")
    item.updated_at = utcnow()
    db.commit()
    assert admin_client.get("/api/v1/pantry/restock").json() == []


def test_the_reminder_goes_out_once_for_the_same_set(admin_client, db):
    pantry_row(db, "toiletpapir", quantity=0, unit="stk", min_quantity=4)
    add_offer(db, "netto", "Toiletpapir 12 ruller", 45.0)
    assert run_restock_check(db) == 1
    assert run_restock_check(db) == 0
    db.commit()
    notification = db.scalar(select(Notification).where(Notification.kind == "restock"))
    assert notification.data["items"] == "toiletpapir"
    assert "Netto" in format_message("restock", notification.data, "da")


def test_a_new_week_reminds_again_of_what_is_still_low(admin_client, db):
    pantry_row(db, "toiletpapir", quantity=0, unit="stk", min_quantity=4)
    assert run_restock_check(db, local_today()) == 1
    assert run_restock_check(db, local_today() + timedelta(days=7)) == 1


def test_reminders_can_be_switched_off_per_user(admin_client, db):
    settings = admin_client.get("/api/v1/me/settings").json()
    settings["features"]["restock"] = False
    admin_client.put("/api/v1/me/settings", json=settings)
    pantry_row(db, "toiletpapir", quantity=0, unit="stk", min_quantity=4)
    assert admin_client.get("/api/v1/pantry/restock").status_code == 403
    assert run_restock_check(db) == 0


def test_what_someone_keeps_in_stock_gets_crawled(admin_client, db):
    from app.crawlers.runner import build_terms

    pantry_row(db, "Toiletpapir", quantity=4, unit="stk", min_quantity=4)
    pantry_row(db, "gave til farmor", quantity=1, unit="stk")
    terms = build_terms(db, 150)
    assert "toiletpapir" in terms
    # Only what someone chose to watch goes out to the sites as a search.
    assert "gave til farmor" not in terms


# Buy signals


def track(client, query, priority=0):
    client.put("/api/v1/me/items", json={"query": query, "priority": priority})


def test_a_low_price_on_a_tracked_item_says_buy_once(admin_client, db):
    add_history(db, "kaffe formalet", "netto", HISTORY)
    add_offer(db, "netto", "Kaffe formalet", 23.0, unit_price=23.0, unit_basis="kg")
    track(admin_client, "kaffe")
    assert run_buy_signals(db) == 1
    assert run_buy_signals(db) == 0
    notification = db.scalar(select(Notification).where(Notification.kind == "buy_now"))
    assert notification.data["store"] == "Netto"
    assert "under den typiske" in format_message("buy_now", notification.data, "da")


def test_a_lower_price_on_the_same_offer_is_news_again(admin_client, db):
    add_history(db, "kaffe formalet", "netto", HISTORY)
    offer = add_offer(db, "netto", "Kaffe formalet", 23.0, unit_price=23.0, unit_basis="kg")
    track(admin_client, "kaffe")
    assert run_buy_signals(db) == 1
    offer.price, offer.unit_price = 19.0, 19.0
    db.commit()
    assert run_buy_signals(db) == 1


def test_no_history_means_no_signal(admin_client, db):
    """A new install has nothing to compare with, and must not make something up."""
    add_offer(db, "netto", "Kaffe formalet", 23.0, unit_price=23.0, unit_basis="kg")
    track(admin_client, "kaffe")
    assert run_buy_signals(db) == 0


def test_a_normal_price_says_nothing(admin_client, db):
    add_history(db, "kaffe formalet", "netto", HISTORY)
    add_offer(db, "netto", "Kaffe formalet", 55.0, unit_price=55.0, unit_basis="kg")
    track(admin_client, "kaffe")
    assert run_buy_signals(db) == 0


def test_buy_signals_never_touch_an_allergy(admin_client, db):
    set_household(db, allergies=["nødder"])
    add_history(db, "peanuts salte", "netto", HISTORY)
    add_offer(db, "netto", "Peanuts salte", 10.0, unit_price=10.0, unit_basis="kg")
    track(admin_client, "peanuts")
    assert run_buy_signals(db) == 0


def test_what_the_household_keeps_buying_is_watched_without_being_asked(admin_client, db):
    for days_ago in (61, 40, 19):
        buy_by_hand(db, "kaffe", days_ago)
    add_history(db, "kaffe formalet", "netto", HISTORY)
    add_offer(db, "netto", "Kaffe formalet", 23.0, unit_price=23.0, unit_basis="kg")
    assert run_buy_signals(db) == 1


def test_buy_signals_can_be_switched_off_per_user(admin_client, db):
    settings = admin_client.get("/api/v1/me/settings").json()
    settings["features"]["buy_signals"] = False
    admin_client.put("/api/v1/me/settings", json=settings)
    add_history(db, "kaffe formalet", "netto", HISTORY)
    add_offer(db, "netto", "Kaffe formalet", 23.0, unit_price=23.0, unit_basis="kg")
    track(admin_client, "kaffe")
    assert run_buy_signals(db) == 0


def test_the_price_index_knows_the_last_months_low(admin_client, db):
    add_history(db, "kaffe formalet", "netto", [(120, 20), (20, 50), (10, 45)])
    index = admin_client.get("/api/v1/offers/index", params={"q": "kaffe"}).json()
    assert index["lowest"] == 20
    assert index["lowest_30_days"] == 45


# Automatic plans


def test_a_plan_made_by_itself_asks_people_to_look(admin_client, db):
    make_user(db, "anna")
    make_user(db, "bo", settings={"features": {"plan_updates": False}})
    job_auto_plan(db)
    db.commit()
    plans = db.scalars(select(MealPlan)).all()
    assert len(plans) == 1
    told = db.scalars(select(Notification).where(Notification.kind == "plan_ready")).all()
    assert len(told) == 2
    assert told[0].data["plan_id"] == plans[0].id
    assert "Stem på måltiderne" in format_message("plan_ready", told[0].data, "da")


def test_the_automatic_plan_is_not_made_twice(admin_client, db):
    job_auto_plan(db)
    job_auto_plan(db)
    db.commit()
    assert len(db.scalars(select(MealPlan)).all()) == 1
    assert len(db.scalars(select(Notification).where(Notification.kind == "plan_ready")).all()) == 1


def test_people_are_told_only_once_per_plan(admin_client, db):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    row = db.get(MealPlan, plan["id"])
    assert notify_plan_ready(db, row) == 1
    assert notify_plan_ready(db, row) == 0


def test_a_plan_made_by_hand_asks_the_others_and_not_the_one_who_made_it(admin_client, db):
    anna = make_user(db, "anna")
    make_user(db, "bo", settings={"features": {"plan_updates": False}})
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    told = db.scalars(select(Notification).where(Notification.kind == "plan_ready")).all()
    assert [n.user_id for n in told] == [anna.id]
    assert told[0].data["plan_id"] == plan["id"]


def test_a_plan_the_assistant_offered_asks_the_others_too(admin_client, db):
    anna = make_user(db, "anna")
    answer = ask(admin_client, "Hvad skal vi spise næste uge?")
    admin_client.post(f"/api/v1/assistant/messages/{answer['message_id']}/proposals/0", json={"approve": True})
    told = db.scalars(select(Notification).where(Notification.kind == "plan_ready")).all()
    assert [n.user_id for n in told] == [anna.id]


def test_sharing_a_plan_tells_the_person_once(admin_client, db):
    other = Household(name="Naboen", settings={})
    db.add(other)
    db.commit()
    guest = make_user(db, "gæst", household_id=other.id)
    quiet = make_user(db, "stille", household_id=other.id, settings={"features": {"plan_updates": False}})
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    for _ in range(2):
        admin_client.post(f"/api/v1/plans/{plan['id']}/share", json={"user_ids": [guest.id, quiet.id]})
    told = db.scalars(select(Notification).where(Notification.kind == "plan_ready")).all()
    assert [n.user_id for n in told] == [guest.id]


def test_the_hour_is_the_households_and_not_the_containers():
    moment = datetime(2026, 7, 1, 5, 30, tzinfo=UTC)
    assert local_hour(moment) == moment.astimezone(ZoneInfo("Europe/Copenhagen")).hour == 7


def test_the_automatic_plan_is_made_when_ai_is_out_of_reach(admin_client, db):
    job_auto_plan(db)
    db.commit()
    assert db.scalar(select(MealPlan)).ai_used is False


# Prices moving under an open plan


def test_new_offers_change_what_an_open_plan_costs(admin_client, db):
    offer = add_offer(db, "netto", "Hakket oksekød 500 g", 30.0, pack_quantity=500, pack_unit="g", unit_price=60, unit_basis="kg")
    plan = one_day_plan_of(admin_client, db, own_recipe(db))
    before = plan["shopping_list"]["summary"]["total"]
    offer.price, offer.unit_price = 90.0, 180.0
    db.commit()
    assert refresh_open_plans(db) == 1
    db.commit()
    after = admin_client.get(f"/api/v1/plans/{plan['id']}").json()
    assert after["shopping_list"]["summary"]["total"] > before
    told = db.scalar(select(Notification).where(Notification.kind == "list_changed"))
    assert told.data["old"] == before
    assert told.data["new"] == after["shopping_list"]["summary"]["total"]
    assert "er nu" in format_message("list_changed", told.data, "da")


def test_a_small_change_is_not_worth_a_message(admin_client, db):
    offer = add_offer(db, "netto", "Hakket oksekød 500 g", 30.0, pack_quantity=500, pack_unit="g", unit_price=60, unit_basis="kg")
    one_day_plan_of(admin_client, db, own_recipe(db))
    offer.price = 31.0
    db.commit()
    assert refresh_open_plans(db) == 1
    db.commit()
    assert db.scalar(select(Notification).where(Notification.kind == "list_changed")) is None


def test_a_list_someone_has_started_on_is_left_alone(admin_client, db):
    offer = add_offer(db, "netto", "Hakket oksekød 500 g", 30.0, pack_quantity=500, pack_unit="g", unit_price=60, unit_basis="kg")
    plan = one_day_plan_of(admin_client, db, own_recipe(db))
    first = next(i for i in plan["shopping_list"]["items"] if not i["covered_by_pantry"])
    admin_client.patch(f"/api/v1/shopping-items/{first['id']}", json={"checked": True})
    offer.price = 90.0
    db.commit()
    assert refresh_open_plans(db) == 0


def test_manual_items_survive_a_new_price(admin_client, db):
    offer = add_offer(db, "netto", "Hakket oksekød 500 g", 30.0, pack_quantity=500, pack_unit="g", unit_price=60, unit_basis="kg")
    plan = one_day_plan_of(admin_client, db, own_recipe(db))
    admin_client.post(f"/api/v1/shopping-lists/{plan['shopping_list']['id']}/items", json={"name": "Toiletpapir"})
    offer.price = 90.0
    db.commit()
    refresh_open_plans(db)
    db.commit()
    after = admin_client.get(f"/api/v1/plans/{plan['id']}").json()
    assert "Toiletpapir" in {i["name"] for i in after["shopping_list"]["items"]}


# The assistant offers, and a person decides


def ask(client, message):
    response = client.post("/api/v1/assistant", json={"message": message})
    assert response.status_code == 200, response.text
    return response.json()


def test_without_ai_it_offers_to_buy_what_is_missing(admin_client, db):
    own_recipe(db, title="Tacos", ingredients=[{"name": "hakket oksekød", "quantity": 400, "unit": "g"}, {"name": "tortilla", "quantity": 8, "unit": "stk"}])
    pantry_row(db, "tortilla", quantity=8, unit="stk")
    answer = ask(admin_client, "Hvilke varer mangler jeg til tacos?")
    assert answer["engine"] == "local"
    assert "hakket oksekød" in answer["answer"]
    assert "tortilla" not in answer["answer"]
    assert answer["proposals"] == [{"type": "add_to_shopping_list", "items": ["hakket oksekød"], "status": "open"}]
    # Nothing has happened yet.
    assert admin_client.get("/api/v1/shopping-lists").json() == []


def test_approving_does_it_and_only_once(admin_client, db):
    own_recipe(db, title="Tacos")
    answer = ask(admin_client, "Hvad mangler til tacos?")
    url = f"/api/v1/assistant/messages/{answer['message_id']}/proposals/0"
    done = admin_client.post(url, json={"approve": True})
    assert done.status_code == 200
    assert done.json()["status"] == "done"
    assert done.json()["result"]["added"] == ["hakket oksekød"]
    lists = admin_client.get("/api/v1/shopping-lists").json()
    assert len(lists) == 1
    items = admin_client.get(f"/api/v1/shopping-lists/{lists[0]['id']}").json()["items"]
    assert [i["name"] for i in items] == ["hakket oksekød"]
    assert admin_client.post(url, json={"approve": True}).status_code == 409
    assert len(admin_client.get(f"/api/v1/shopping-lists/{lists[0]['id']}").json()["items"]) == 1


def test_declining_does_nothing(admin_client, db):
    own_recipe(db, title="Tacos")
    answer = ask(admin_client, "Hvad mangler til tacos?")
    url = f"/api/v1/assistant/messages/{answer['message_id']}/proposals/0"
    assert admin_client.post(url, json={"approve": False}).json()["status"] == "declined"
    assert admin_client.get("/api/v1/shopping-lists").json() == []
    history = admin_client.get("/api/v1/assistant/history").json()
    assert history[-1]["proposals"][0]["status"] == "declined"


def test_only_the_person_asked_can_answer(admin_client, db):
    own_recipe(db, title="Tacos")
    answer = ask(admin_client, "Hvad mangler til tacos?")
    make_user(db, "anna")
    url = f"/api/v1/assistant/messages/{answer['message_id']}/proposals/0"
    assert login_as("anna").post(url, json={"approve": True}).status_code == 404
    assert admin_client.post(f"/api/v1/assistant/messages/{answer['message_id']}/proposals/9", json={"approve": True}).status_code == 404


def test_it_offers_a_plan_when_there_is_none_with_the_budget_asked_for(admin_client, db):
    answer = ask(admin_client, "Hvad skal vi spise næste uge? Kan vi holde os under 900 kr?")
    assert answer["proposals"][0]["type"] == "make_plan"
    assert answer["proposals"][0]["budget"] == 900
    assert db.scalar(select(MealPlan)) is None
    done = admin_client.post(f"/api/v1/assistant/messages/{answer['message_id']}/proposals/0", json={"approve": True}).json()
    plan = admin_client.get(f"/api/v1/plans/{done['result']['plan_id']}").json()
    assert plan["budget"] == 900
    assert plan["meals"]


def test_with_a_plan_it_answers_from_the_plan_and_does_not_offer_another(admin_client, db):
    make_plan(admin_client)
    answer = ask(admin_client, "Hvad skal vi spise næste uge? Kan vi holde os under 90000 kr?")
    assert answer["proposals"] == []
    assert "det holder under 90000 kr" in answer["answer"]


def test_the_model_can_only_offer_and_what_it_offers_is_filtered(admin_client, db, fake_openai):  # noqa: F811
    set_household(db, allergies=["nødder"])
    fake_openai([ai_answer("Jeg kan lægge kaffe på listen.", [
        {"kind": "shopping_items", "items": ["Kaffe", "Peanuts", "kaffe"]},
        {"kind": "price_alert", "query": "Smør", "max_price": 12, "basis": "pack"},
    ])])
    answer = ask(admin_client, "Vi mangler kaffe og peanuts")
    assert answer["engine"] == "ai"
    assert answer["proposals"][0] == {"type": "add_to_shopping_list", "items": ["Kaffe"], "status": "open"}
    assert answer["proposals"][1]["type"] == "create_price_alert"
    # Being offered changes nothing.
    assert admin_client.get("/api/v1/shopping-lists").json() == []
    assert admin_client.get("/api/v1/alerts").json() == []

    done = admin_client.post(f"/api/v1/assistant/messages/{answer['message_id']}/proposals/1", json={"approve": True})
    assert done.status_code == 200
    assert [a["query"] for a in admin_client.get("/api/v1/alerts").json()] == ["Smør"]


def test_an_allergy_set_after_the_offer_still_counts(admin_client, db):
    own_recipe(db, title="Tacos")
    answer = ask(admin_client, "Hvad mangler til tacos?")
    set_household(db, allergies=["oksekød"])
    db.expire_all()
    done = admin_client.post(f"/api/v1/assistant/messages/{answer['message_id']}/proposals/0", json={"approve": True}).json()
    assert done["result"]["added"] == []


def test_a_model_cannot_offer_more_than_a_few_things(admin_client, db, fake_openai):  # noqa: F811
    fake_openai([ai_answer("Færdig.", [{"kind": "shopping_items", "items": ["Kaffe"]}] * 5)])
    assert len(ask(admin_client, "Køb kaffe")["proposals"]) == 3


def test_offers_are_only_mentioned_where_they_exist(admin_client, db, fake_openai):  # noqa: F811
    model = fake_openai([ai_answer("x")], [ai_answer("y")])
    admin_client.post("/api/v1/assistant", json={"message": "?", "mode": "direct"})
    admin_client.post("/api/v1/assistant", json={"message": "?", "mode": "auto"})
    assert "put it in offers" not in model.calls[0]["instructions"]
    assert "put it in offers" in model.calls[1]["instructions"]


def test_a_model_cannot_offer_nonsense(admin_client, db, fake_openai):  # noqa: F811
    fake_openai([ai_answer("Nej.", [
        {"kind": "price_alert", "query": "x", "max_price": -1, "basis": "pack"},
        {"kind": "meal_plan", "period": "forever"},
        {"kind": "delete_everything"},
    ])])
    assert ask(admin_client, "Lav noget")["proposals"] == []


# Family taste


def _voted_meals(db, plan_id_title_pairs):
    household = household_of_admin(db)
    plan = MealPlan(household_id=household.id, start_date=local_today(), end_date=local_today(), meal_types=["dinner"])
    db.add(plan)
    db.flush()
    meals = []
    for title in plan_id_title_pairs:
        meal = PlannedMeal(plan_id=plan.id, date=local_today(), meal_type="dinner", title=title)
        db.add(meal)
        meals.append(meal)
    db.flush()
    return meals


def test_the_one_who_votes_on_everything_does_not_outweigh_the_rest(admin_client, db):
    eager = make_user(db, "eager")
    quiet = make_user(db, "quiet")
    meals = _voted_meals(db, ["Fiskegryde"] * 12)
    for meal in meals[:10]:
        db.add(MealVote(meal_id=meal.id, user_id=eager.id, vote="yes"))
    for meal in meals[10:]:
        db.add(MealVote(meal_id=meal.id, user_id=quiet.id, vote="no"))
    db.commit()
    # By plain counting this would have been a strong yes.
    assert learn_taste(db, household_of_admin(db).id).weights["fiskegryde"] == pytest.approx(0.5)


def test_taste_is_shown_per_person(admin_client, db):
    anna = make_user(db, "anna")
    meals = _voted_meals(db, ["Fiskegryde", "Fiskegryde", "Pitabrød", "Pitabrød"])
    for meal, vote in zip(meals, ["no", "no", "yes", "yes"], strict=True):
        db.add(MealVote(meal_id=meal.id, user_id=anna.id, vote=vote))
    db.commit()
    members = admin_client.get("/api/v1/household/taste").json()["members"]
    assert members[0]["name"] == "Anna"
    assert members[0]["rejected"][0]["term"] == "fiskegryde"
    assert members[0]["liked"][0]["term"] == "pitabrød"


def test_a_guest_on_a_shared_plan_is_not_shown_by_name(admin_client, db):
    other = Household(name="Naboen", settings={})
    db.add(other)
    db.commit()
    guest = make_user(db, "gæst", household_id=other.id)
    meals = _voted_meals(db, ["Fiskegryde", "Fiskegryde"])
    for meal in meals:
        db.add(MealVote(meal_id=meal.id, user_id=guest.id, vote="yes"))
    db.commit()
    taste = admin_client.get("/api/v1/household/taste").json()
    assert taste["members"] == []
    assert taste["liked"][0]["term"] == "fiskegryde"


# Taking your data along, and leaving


def test_an_export_has_what_is_yours_and_no_secrets(admin_client, db):
    admin_client.post("/api/v1/assistant", json={"message": "Hvad koster kaffe?"})
    admin_client.post("/api/v1/alerts", json={"query": "smør", "max_price": 12})
    pantry_row(db, "ost", quantity=1, unit="stk")
    admin_client.post("/api/v1/me/tokens", json={"name": "HA"})
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    admin_client.post(f"/api/v1/meals/{plan['meals'][0]['id']}/vote", json={"vote": "yes"})

    response = admin_client.get("/api/v1/me/export")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    data = response.json()
    assert data["profile"]["username"] == "admin"
    assert data["price_alerts"][0]["query"] == "smør"
    assert data["votes"][0]["vote"] == "yes"
    assert data["assistant_chats"]
    assert data["household"]["pantry"][0]["name"] == "ost"
    assert data["household"]["plans"][0]["meals"]
    text = response.text.lower()
    assert "password" not in text
    assert "argon2" not in text
    assert "token_hash" not in text


def test_an_export_has_nothing_from_other_households(admin_client, db):
    other = Household(name="Naboen", settings={})
    db.add(other)
    db.commit()
    pantry_row(db, "naboens hemmelige ost", household=other)
    assert "naboens hemmelige ost" not in admin_client.get("/api/v1/me/export").text


def test_leaving_removes_the_person_and_leaves_the_household_its_plans(admin_client, db):
    anna = make_user(db, "anna")
    anna_id = anna.id
    client = login_as("anna")
    plan = make_plan(client, mode="days_ahead", days_ahead=1)
    client.post(f"/api/v1/meals/{plan['meals'][0]['id']}/vote", json={"vote": "yes"})
    client.post("/api/v1/alerts", json={"query": "smør", "max_price": 12})
    client.post("/api/v1/assistant", json={"message": "Hej"})

    assert client.post("/api/v1/me/delete", json={"confirm_username": "forkert"}).status_code == 422
    assert client.post("/api/v1/me/delete", json={"confirm_username": "Anna"}).status_code == 200

    db.expire_all()
    assert db.get(User, anna_id) is None
    assert db.scalar(select(ChatMessage).where(ChatMessage.user_id == anna_id)) is None
    assert db.scalar(select(MealVote).where(MealVote.user_id == anna_id)) is None
    remaining = db.get(MealPlan, plan["id"])
    assert remaining is not None
    assert remaining.created_by is None
    # Still a Home Assistant user, so opening the app again starts them afresh.
    assert client.get("/api/v1/auth/me").json()["username"] == "anna"
    assert client.get("/api/v1/assistant/history").json() == []
    assert client.get("/api/v1/alerts").json() == []


def test_an_administrator_can_leave_too(admin_client, db):
    """Administrators are Home Assistant's, so there is always one to be had."""
    assert admin_client.post("/api/v1/me/delete", json={"confirm_username": "admin"}).status_code == 200
    assert admin_client.get("/api/v1/auth/me").json()["role"] == "admin"


def test_old_chats_and_messages_are_not_kept_for_ever(admin_client, db):
    user = db.scalar(select(User).where(User.username == "admin"))
    old = utcnow() - timedelta(days=400)
    db.add(ChatMessage(user_id=user.id, role="user", content="gammel", created_at=old))
    db.add(ChatMessage(user_id=user.id, role="user", content="ny"))
    db.add(Notification(user_id=user.id, kind="waste", data={}, created_at=old))
    db.add(Notification(user_id=user.id, kind="waste", data={}))
    db.commit()
    job_cleanup(db)
    db.commit()
    assert [m.content for m in db.scalars(select(ChatMessage))] == ["ny"]
    assert len(db.scalars(select(Notification)).all()) == 1


def test_keeping_them_for_ever_is_a_choice(admin_client, db):
    set_options(chat_retention_days=0)
    user = db.scalar(select(User).where(User.username == "admin"))
    db.add(ChatMessage(user_id=user.id, role="user", content="gammel", created_at=utcnow() - timedelta(days=4000)))
    db.commit()
    job_cleanup(db)
    db.commit()
    assert db.scalar(select(ChatMessage)) is not None


# Messages


def test_a_waste_message_says_what_to_make_and_by_when(admin_client, db):
    from app.services.household_care import run_waste_check

    pantry_row(db, "hakket kylling", expires_on=local_today() + timedelta(days=1))
    assert run_waste_check(db) == 1
    data = db.scalar(select(Notification).where(Notification.kind == "waste")).data
    message = format_message("waste", data, "da")
    assert message.startswith("Lav ")
    assert "senest i morgen" in message
    assert "hakket kylling" in message


def test_food_already_gone_off_gets_no_dish_suggested(admin_client, db):
    pantry_row(db, "hakket kylling", expires_on=local_today() - timedelta(days=1))
    assert admin_client.get("/api/v1/waste").json()["dishes"] == []


def test_an_empty_row_is_not_about_to_go_off(admin_client, db):
    pantry_row(db, "hakket kylling", quantity=0, unit="g", min_quantity=100, expires_on=local_today())
    assert admin_client.get("/api/v1/waste").json()["items"] == []


def test_messages_carry_no_link_as_the_app_lives_inside_home_assistant():
    assert page_link("plan_ready") is None


def test_messages_read_in_english_too():
    assert "Make Chicken pot by tomorrow" in format_message("waste", {"dish": "Kyllingegryde", "dish_en": "Chicken pot", "days_left": 1, "items": "x"}, "en")
    assert format_message("list_changed", {"old": 100, "new": 150.5}, "en") == "The offers have changed: DKK 100 is now DKK 150.50."
    assert "DKK 23" in format_message("buy_now", {"title": "Kaffe", "price": 23, "store": "Netto", "discount": 0.4}, "en")


# Home Assistant


def ha_headers(client):
    token = client.post("/api/v1/me/tokens", json={"name": "HA"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_home_assistant_hears_what_is_running_low_and_what_to_vote_on(admin_client, db, client):
    headers = ha_headers(admin_client)
    pantry_row(db, "toiletpapir", quantity=0, unit="stk", min_quantity=4)
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    summary = client.get("/api/v1/ha/summary", headers=headers).json()
    assert summary["running_low"][0]["name"] == "toiletpapir"
    assert summary["meals_to_vote"][0]["id"] == plan["meals"][0]["id"]
    assert summary["buy_now"] == []


def test_home_assistant_can_vote_and_then_it_stops_asking(admin_client, db, client):
    headers = ha_headers(admin_client)
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    meal_id = plan["meals"][0]["id"]
    assert client.post(f"/api/v1/ha/meals/{meal_id}/vote", json={"vote": "yes"}, headers=headers).json()["status"] == "accepted"
    assert client.get("/api/v1/ha/summary", headers=headers).json()["meals_to_vote"] == []


def test_home_assistant_can_say_a_meal_was_made(admin_client, db, client):
    headers = ha_headers(admin_client)
    pantry_row(db, "hakket oksekød", quantity=1000, unit="g")
    meal = planned_meal(db, own_recipe(db), 0)
    assert client.post(f"/api/v1/ha/meals/{meal.id}/cooked", json={"cooked": True}, headers=headers).json()["cooked"] is True
    assert pantry_amount(db, "hakket oksekød") == 600


def test_home_assistant_shows_current_buy_signals_only(admin_client, db, client):
    headers = ha_headers(admin_client)
    add_history(db, "kaffe formalet", "netto", HISTORY)
    add_offer(db, "netto", "Kaffe formalet", 23.0, unit_price=23.0, unit_basis="kg")
    track(admin_client, "kaffe")
    run_buy_signals(db)
    assert client.get("/api/v1/ha/summary", headers=headers).json()["buy_now"][0]["query"] == "kaffe"
    row = db.scalar(select(Notification).where(Notification.kind == "buy_now"))
    row.data = {**row.data, "valid_to": (local_today() - timedelta(days=1)).isoformat()}
    db.commit()
    assert client.get("/api/v1/ha/summary", headers=headers).json()["buy_now"] == []


def test_home_assistant_can_keep_a_minimum(admin_client, db, client):
    headers = ha_headers(admin_client)
    client.post("/api/v1/ha/pantry", json={"name": "Toiletpapir", "quantity": 1, "unit": "stk", "min_quantity": 4}, headers=headers)
    assert client.get("/api/v1/ha/summary", headers=headers).json()["running_low"][0]["name"] == "Toiletpapir"


def test_home_assistant_cannot_reach_another_households_meal(admin_client, db, client):
    headers = ha_headers(admin_client)
    other = Household(name="Naboen", settings={})
    db.add(other)
    db.flush()
    plan = MealPlan(household_id=other.id, start_date=local_today(), end_date=local_today(), meal_types=["dinner"])
    db.add(plan)
    db.flush()
    meal = PlannedMeal(plan_id=plan.id, date=local_today(), meal_type="dinner", title="Hemmelig ret")
    db.add(meal)
    db.commit()
    assert client.post(f"/api/v1/ha/meals/{meal.id}/vote", json={"vote": "yes"}, headers=headers).status_code == 404
    assert client.post(f"/api/v1/ha/meals/{meal.id}/cooked", json={"cooked": False}, headers=headers).status_code == 404


# The worker


def test_the_worker_runs_the_new_jobs_and_reprices_only_after_a_crawl(admin_client, db, monkeypatch):
    from app import worker

    monkeypatch.setattr(worker, "job_crawl", lambda session: None)
    first = worker.run_due_jobs()
    assert {"pantry", "restock", "buy_signals"} <= set(first)
    assert "reprice" in worker.run_due_jobs()
    assert "reprice" not in worker.run_due_jobs()


def test_admins_can_switch_the_new_jobs_off(admin_client, db, monkeypatch):
    from app import worker

    monkeypatch.setattr(worker, "job_crawl", lambda session: None)
    # Buy signals are switched off in the app's configuration in Home Assistant.
    set_options(buy_signals=False)
    schedule = get_schedule(db)
    schedule.reprice_after_crawl = False
    save_schedule(db, schedule)
    db.commit()
    worker.run_due_jobs()
    ran = worker.run_due_jobs()
    assert "buy_signals" not in ran
    assert "reprice" not in ran




# Both languages


def test_every_built_in_recipe_has_its_steps_in_english():
    from app.services.builtin_recipes import BUILTIN_RECIPES
    from app.services.builtin_recipes_en import STEPS_EN

    assert {r.title for r in BUILTIN_RECIPES} == set(STEPS_EN)
    for recipe in BUILTIN_RECIPES:
        assert len(STEPS_EN[recipe.title]) == len(recipe.steps), recipe.title
        assert all(step.strip() for step in STEPS_EN[recipe.title])


def test_the_api_gives_english_steps(admin_client, db):
    recipes = admin_client.get("/api/v1/recipes", params={"q": "Lasagne"}).json()
    assert recipes[0]["steps_en"][0].startswith("Brown the meat")


def test_an_install_from_before_gets_english_steps_at_the_next_start(admin_client, db):
    from app.services.recipes import seed_builtin_recipes

    recipe = db.scalar(select(Recipe).where(Recipe.title == "Lasagne"))
    recipe.steps_en = None
    recipe.steps = ["Min egen måde."]
    db.commit()
    assert seed_builtin_recipes(db) == 0
    db.commit()
    db.expire_all()
    recipe = db.scalar(select(Recipe).where(Recipe.title == "Lasagne"))
    assert recipe.steps_en[0].startswith("Brown the meat")
    # And nothing else about the row was touched.
    assert recipe.steps == ["Min egen måde."]
