"""Dishes people put on a day themselves, warnings about the same dish nearby, and bought food."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.core.db import local_today
from app.models import MealPlan, Notification, PlannedMeal
from app.services.notifications import format_message
from app.worker import job_auto_plan

from .conftest import add_offer, login_as, make_user
from .test_new_recipes import mealie, run_worker, send  # noqa: F401 (the fixture is found by its name)
from .test_planning import NEXT_MONDAY, _other_household, make_plan, set_household

TODAY = local_today()


def recipe_id(client, title: str) -> int:
    found = client.get("/api/v1/recipes", params={"q": title}).json()
    assert found, title
    return found[0]["id"]


def add(client, title: str, day, meal_type="dinner", replace=False, expect=200) -> dict:
    response = client.post(
        "/api/v1/plans/add-meal",
        json={"recipe_id": recipe_id(client, title), "date": day.isoformat(), "meal_type": meal_type, "replace": replace},
    )
    assert response.status_code == expect, response.text
    return response.json()


def meal_in(plan: dict, meal_id: int) -> dict:
    return next(m for m in plan["meals"] if m["id"] == meal_id)


def vote(client, meal_id: int, value: str):
    return client.post(f"/api/v1/meals/{meal_id}/vote", json={"vote": value})


# Choosing a day


def test_a_day_without_a_plan_gets_a_plan_for_its_week(admin_client, db):
    day = NEXT_MONDAY + timedelta(days=3)
    result = add(admin_client, "Spaghetti bolognese", day)
    plan = result["plan"]
    assert (plan["start_date"], plan["end_date"]) == (NEXT_MONDAY.isoformat(), (NEXT_MONDAY + timedelta(days=6)).isoformat())
    assert [m["title"] for m in plan["meals"]] == ["Spaghetti bolognese"]
    meal = meal_in(plan, result["meal_id"])
    assert meal["date"] == day.isoformat()
    assert meal["added_by"]["name"] == "Admin"
    assert meal["reasons"][0]["code"] == "chosen_by_hand"
    # Its food goes on the plan's list, as with any other dish.
    assert any("oksekød" in item["name"] for item in plan["shopping_list"]["items"])
    # The rest of the week is empty, and can be filled around it.
    assert plan["missing_slots"] == 6


def test_a_day_in_a_plan_goes_into_that_plan(admin_client):
    plan = make_plan(admin_client)
    day = NEXT_MONDAY + timedelta(days=2)
    result = add(admin_client, "Lasagne", day, meal_type="lunch")
    assert result["plan_id"] == plan["id"]
    assert len(result["plan"]["meals"]) == len(plan["meals"]) + 1


def test_choosing_it_counts_as_your_yes_and_the_others_still_vote(admin_client, db):
    make_user(db, "mor")
    set_household(db, voting_rule="majority")
    make_user(db, "far")
    result = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    meal = meal_in(result["plan"], result["meal_id"])
    assert meal["status"] == "pending"
    assert [v["name"] for v in meal["votes"]] == ["Admin"]
    plan = login_as("mor").post(f"/api/v1/meals/{meal['id']}/vote", json={"vote": "yes"}).json()
    assert meal_in(plan, meal["id"])["status"] == "accepted"


def test_with_one_vote_deciding_it_is_settled_at_once(admin_client):
    result = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    assert meal_in(result["plan"], result["meal_id"])["status"] == "accepted"


def test_the_others_are_told(admin_client, db):
    make_user(db, "mor")
    make_user(db, "far", settings={"features": {"plan_updates": False}})
    add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    told = db.scalars(select(Notification).where(Notification.kind == "meal_added")).all()
    assert len(told) == 1
    assert "Admin har sat Spaghetti bolognese på" in format_message("meal_added", told[0].data, "da")


def test_a_taken_place_is_not_overwritten_unless_asked(admin_client):
    first = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    add(admin_client, "Lasagne", NEXT_MONDAY, expect=409)
    replaced = add(admin_client, "Lasagne", NEXT_MONDAY, replace=True)
    assert replaced["meal_id"] == first["meal_id"]
    assert [m["title"] for m in replaced["plan"]["meals"]] == ["Lasagne"]


def test_a_refused_dish_does_not_hold_its_place(admin_client):
    first = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    vote(admin_client, first["meal_id"], "no")
    second = add(admin_client, "Lasagne", NEXT_MONDAY)
    assert second["meal_id"] != first["meal_id"]


def test_an_allergen_cannot_be_chosen_by_hand(admin_client, db):
    set_household(db, allergies=["gluten"])
    response = admin_client.post(
        "/api/v1/plans/add-meal",
        json={"recipe_id": recipe_id(admin_client, "Spaghetti bolognese"), "date": NEXT_MONDAY.isoformat(), "meal_type": "dinner"},
    )
    assert response.status_code == 409
    assert response.json()["detail"].startswith("allergy:")


def test_the_past_cannot_be_planned(admin_client):
    add(admin_client, "Spaghetti bolognese", TODAY - timedelta(days=1), expect=409)


def test_a_skipped_day_is_eaten_on_after_all(admin_client):
    plan = make_plan(admin_client, skip_dates=[(NEXT_MONDAY + timedelta(days=1)).isoformat()])
    result = add(admin_client, "Lasagne", NEXT_MONDAY + timedelta(days=1))
    assert result["plan_id"] == plan["id"]
    day = next(d for d in result["plan"]["days"] if d["date"] == (NEXT_MONDAY + timedelta(days=1)).isoformat())
    assert day["skipped"] is False


def test_the_empty_days_can_be_filled_around_it(admin_client):
    result = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY + timedelta(days=3))
    filled = admin_client.post(f"/api/v1/plans/{result['plan_id']}/fill").json()
    assert filled["added"] == 6
    titles = [m["title"] for m in filled["plan"]["meals"] if m["meal_type"] == "dinner"]
    assert len(titles) == 7 and len(set(titles)) == 7
    assert filled["plan"]["missing_slots"] == 0


def test_the_weekly_plan_fills_in_around_a_chosen_dish(admin_client, db):
    make_user(db, "mor")
    add(admin_client, "Spaghetti bolognese", NEXT_MONDAY + timedelta(days=3))
    job_auto_plan(db)
    db.commit()
    plans = db.scalars(select(MealPlan)).all()
    assert len(plans) == 1
    assert len([m for m in plans[0].meals if m.meal_type == "dinner"]) == 7
    assert db.scalars(select(Notification).where(Notification.kind == "plan_ready")).all()


# The same dish nearby


def test_the_same_dish_nearby_is_shown_on_both(admin_client):
    first = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    second = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY + timedelta(days=4))
    plan = second["plan"]
    assert [x["date"] for x in meal_in(plan, first["meal_id"])["also_planned"]] == [(NEXT_MONDAY + timedelta(days=4)).isoformat()]
    assert [x["date"] for x in meal_in(plan, second["meal_id"])["also_planned"]] == [NEXT_MONDAY.isoformat()]


def test_the_warning_follows_the_households_repeat_setting(admin_client, db):
    set_household(db, repeat_avoid_days=2)
    add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    second = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY + timedelta(days=4))
    assert meal_in(second["plan"], second["meal_id"])["also_planned"] == []


def test_choosing_a_day_shows_where_it_is_planned_and_what_is_there(admin_client):
    add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    add(admin_client, "Lasagne", NEXT_MONDAY + timedelta(days=1))
    rid = recipe_id(admin_client, "Spaghetti bolognese")
    view = admin_client.get(f"/api/v1/recipes/{rid}/planning", params={"day": (NEXT_MONDAY + timedelta(days=1)).isoformat(), "meal_type": "dinner"}).json()
    assert [p["date"] for p in view["planned"]] == [NEXT_MONDAY.isoformat()]
    assert len(view["too_close"]) == 1
    assert view["slot"]["title"] == "Lasagne"
    assert view["window_days"] == 21


def test_a_refused_dish_is_not_called_planned(admin_client):
    first = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    vote(admin_client, first["meal_id"], "no")
    rid = recipe_id(admin_client, "Spaghetti bolognese")
    assert admin_client.get(f"/api/v1/recipes/{rid}/planning").json()["planned"] == []


# Bought food


def test_bought_food_cannot_be_voted_off(admin_client, db):
    make_user(db, "mor")
    result = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    meal = result["meal_id"]
    admin_client.post(f"/api/v1/meals/{meal}/bought", json={"bought": "full"})
    response = vote(login_as("mor"), meal, "no")
    assert (response.status_code, response.json()["detail"]) == (409, "meal_bought")
    assert admin_client.post(f"/api/v1/meals/{meal}/decide", json={"vote": "no"}).status_code == 409
    option = admin_client.get(f"/api/v1/meals/{meal}/options").json()[0]
    assert admin_client.post(f"/api/v1/meals/{meal}/replace", json={"recipe_id": option["recipe_id"]}).status_code == 409
    skip = admin_client.patch(f"/api/v1/plans/{result['plan_id']}/days/{NEXT_MONDAY.isoformat()}", json={"skipped": True})
    assert skip.status_code == 409
    assert vote(admin_client, meal, "yes").status_code == 200


def test_partly_bought_food_can_still_be_voted_off(admin_client):
    result = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    meal = result["meal_id"]
    plan = admin_client.post(f"/api/v1/meals/{meal}/bought", json={"bought": "partial"}).json()
    assert meal_in(plan, meal)["bought"] == "partial"
    assert meal_in(vote(admin_client, meal, "no").json(), meal)["status"] == "rejected"


def test_shopping_the_plans_list_marks_its_meals_as_bought(admin_client, db):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=2)
    rejected = plan["meals"][0]["id"]
    vote(admin_client, rejected, "no")
    response = admin_client.post(f"/api/v1/shopping-lists/{plan['shopping_list']['id']}/complete", json={})
    assert response.status_code == 200, response.text
    after = admin_client.get(f"/api/v1/plans/{plan['id']}").json()
    assert {m["id"]: m["bought"] for m in after["meals"]} == {m["id"]: ("none" if m["id"] == rejected else "full") for m in plan["meals"]}


def test_a_new_dish_starts_out_not_bought(admin_client):
    first = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    admin_client.post(f"/api/v1/meals/{first['meal_id']}/bought", json={"bought": "partial"})
    replaced = add(admin_client, "Lasagne", NEXT_MONDAY, replace=True)
    assert meal_in(replaced["plan"], first["meal_id"])["bought"] == "none"


# Moving


def test_a_meal_moves_within_its_plan(admin_client):
    result = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    meal = result["meal_id"]
    admin_client.post(f"/api/v1/meals/{meal}/bought", json={"bought": "full"})
    moved = admin_client.post(f"/api/v1/meals/{meal}/move", json={"date": (NEXT_MONDAY + timedelta(days=2)).isoformat()})
    assert moved.status_code == 200, moved.text
    after = meal_in(moved.json(), meal)
    assert (after["date"], after["bought"], after["status"]) == ((NEXT_MONDAY + timedelta(days=2)).isoformat(), "full", "accepted")


def test_a_meal_does_not_move_onto_another_or_out_of_its_plan(admin_client):
    first = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    add(admin_client, "Lasagne", NEXT_MONDAY + timedelta(days=1))
    onto = admin_client.post(f"/api/v1/meals/{first['meal_id']}/move", json={"date": (NEXT_MONDAY + timedelta(days=1)).isoformat()})
    assert (onto.status_code, onto.json()["detail"]) == (409, "slot_taken")
    away = admin_client.post(f"/api/v1/meals/{first['meal_id']}/move", json={"date": (NEXT_MONDAY + timedelta(days=9)).isoformat()})
    assert (away.status_code, away.json()["detail"]) == (409, "date_not_in_plan")


def test_moving_a_sent_meal_moves_it_in_mealie_without_touching_its_list(admin_client, db, mealie):  # noqa: F811
    result = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    send(admin_client, result["plan_id"])
    run_worker(db)
    assert [e["date"] for e in mealie.dishes] == [NEXT_MONDAY.isoformat()]
    listed = list(mealie.listed)

    plan = admin_client.post(f"/api/v1/meals/{result['meal_id']}/move", json={"date": (NEXT_MONDAY + timedelta(days=3)).isoformat()}).json()
    # Nothing goes to Mealie before the button, and the button knows there is a change.
    assert [e["date"] for e in mealie.dishes] == [NEXT_MONDAY.isoformat()]
    assert plan["mealie"]["to_send"] == 1
    send(admin_client, result["plan_id"])
    run_worker(db)
    assert [e["date"] for e in mealie.dishes] == [(NEXT_MONDAY + timedelta(days=3)).isoformat()]
    assert mealie.listed == listed
    assert admin_client.get(f"/api/v1/plans/{result['plan_id']}").json()["mealie"]["to_send"] == 0


def test_a_meal_sent_before_dates_were_recorded_still_moves(admin_client, db, mealie):  # noqa: F811
    result = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    send(admin_client, result["plan_id"])
    run_worker(db)
    row = db.get(PlannedMeal, result["meal_id"])
    row.mealie_sent = {k: v for k, v in row.mealie_sent.items() if k != "date"}
    db.commit()
    admin_client.post(f"/api/v1/meals/{result['meal_id']}/move", json={"date": (NEXT_MONDAY + timedelta(days=1)).isoformat()})
    send(admin_client, result["plan_id"])
    run_worker(db)
    assert [e["date"] for e in mealie.dishes] == [(NEXT_MONDAY + timedelta(days=1)).isoformat()]


# From an offer


def test_an_offer_shows_the_dishes_that_use_it(admin_client, db):
    offer = add_offer(db, "netto", "Hakket oksekød 8-12%", 35.0, pack_quantity=500, pack_unit="g")
    dishes = admin_client.get(f"/api/v1/offers/{offer.id}/dishes").json()
    titles = {d["title"] for d in dishes}
    assert {"Spaghetti bolognese", "Lasagne"} <= titles
    assert not any("laks" in t.lower() for t in titles)


def test_an_offer_never_leads_to_an_allergen(admin_client, db):
    set_household(db, allergies=["gluten"])
    offer = add_offer(db, "netto", "Hakket oksekød 8-12%", 35.0)
    titles = {d["title"] for d in admin_client.get(f"/api/v1/offers/{offer.id}/dishes").json()}
    assert "Spaghetti bolognese" not in titles and "Lasagne" not in titles


# Households


def test_another_household_cannot_touch_these(admin_client, db):
    result = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    make_user(db, "nabo", household_id=_other_household(db).id)
    neighbour = login_as("nabo")
    meal = result["meal_id"]
    assert neighbour.post(f"/api/v1/meals/{meal}/bought", json={"bought": "full"}).status_code == 404
    assert neighbour.post(f"/api/v1/meals/{meal}/move", json={"date": NEXT_MONDAY.isoformat()}).status_code == 404
    assert neighbour.post(f"/api/v1/plans/{result['plan_id']}/fill").status_code == 404
    # The neighbour planning the same dish makes a plan of their own, and sees nothing of ours.
    own = neighbour.post(
        "/api/v1/plans/add-meal",
        json={"recipe_id": recipe_id(admin_client, "Spaghetti bolognese"), "date": NEXT_MONDAY.isoformat(), "meal_type": "dinner"},
    ).json()
    assert own["plan_id"] != result["plan_id"]
    assert meal_in(own["plan"], own["meal_id"])["also_planned"] == []


def test_two_dishes_can_trade_days(admin_client):
    first = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    second = add(admin_client, "Lasagne", NEXT_MONDAY + timedelta(days=3))
    admin_client.post(f"/api/v1/meals/{second['meal_id']}/bought", json={"bought": "full"})
    moved = admin_client.post(f"/api/v1/meals/{first['meal_id']}/move", json={"date": (NEXT_MONDAY + timedelta(days=3)).isoformat(), "swap": True})
    assert moved.status_code == 200, moved.text
    plan = moved.json()
    assert meal_in(plan, first["meal_id"])["date"] == (NEXT_MONDAY + timedelta(days=3)).isoformat()
    # Bought food can still change day, which is what swapping does to it.
    assert (meal_in(plan, second["meal_id"])["date"], meal_in(plan, second["meal_id"])["bought"]) == (NEXT_MONDAY.isoformat(), "full")


def test_swapping_moves_both_in_mealie(admin_client, db, mealie):  # noqa: F811
    first = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    add(admin_client, "Lasagne", NEXT_MONDAY + timedelta(days=1))
    send(admin_client, first["plan_id"])
    run_worker(db)
    admin_client.post(f"/api/v1/meals/{first['meal_id']}/move", json={"date": (NEXT_MONDAY + timedelta(days=1)).isoformat(), "swap": True})
    send(admin_client, first["plan_id"])
    run_worker(db)
    days = {e["title"]: e["date"] for e in mealie.dishes}
    assert days == {"Spaghetti bolognese": (NEXT_MONDAY + timedelta(days=1)).isoformat(), "Lasagne": NEXT_MONDAY.isoformat()}


# Leftovers planned on purpose


def item_amount(plan: dict, word: str) -> float:
    return sum(i["quantity"] or 0 for i in plan["shopping_list"]["items"] if word in i["name"])


def test_leftovers_cook_more_and_go_on_tomorrows_lunch(admin_client):
    dinner = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    before = dinner["plan"]
    meal = meal_in(before, dinner["meal_id"])
    plan = admin_client.post(f"/api/v1/meals/{dinner['meal_id']}/leftovers", json={}).json()
    cooked = meal_in(plan, dinner["meal_id"])
    lunch = next(m for m in plan["meals"] if m["leftover_of"])
    assert (lunch["date"], lunch["meal_type"], lunch["title"]) == ((NEXT_MONDAY + timedelta(days=1)).isoformat(), "lunch", "Rester: Spaghetti bolognese")
    assert lunch["recipe_id"] is None and lunch["status"] == cooked["status"]
    assert cooked["leftover_portions"] == 2
    assert cooked["leftovers"][0]["id"] == lunch["id"]
    # Twice the people, so twice the mince, and the lunch buys nothing of its own.
    assert item_amount(plan, "oksekød") == 2 * item_amount(before, "oksekød")
    assert cooked["estimated_cost"] == round(2 * meal["estimated_cost"], 2)


def test_leftovers_follow_the_meal_and_are_not_voted_on(admin_client, db):
    make_user(db, "mor")
    set_household(db, voting_rule="majority")
    dinner = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    plan = admin_client.post(f"/api/v1/meals/{dinner['meal_id']}/leftovers", json={}).json()
    lunch = next(m for m in plan["meals"] if m["leftover_of"])
    assert lunch["status"] == "pending"
    assert vote(admin_client, lunch["id"], "yes").status_code == 409
    plan = vote(login_as("mor"), dinner["meal_id"], "yes").json()
    assert meal_in(plan, lunch["id"])["status"] == "accepted"
    plan = vote(login_as("mor"), dinner["meal_id"], "no").json()
    assert meal_in(plan, lunch["id"])["status"] == "rejected"


def test_leftovers_can_be_called_off_from_either_end(admin_client):
    dinner = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    before = dinner["plan"]
    plan = admin_client.post(f"/api/v1/meals/{dinner['meal_id']}/leftovers", json={}).json()
    lunch = next(m for m in plan["meals"] if m["leftover_of"])
    plan = admin_client.delete(f"/api/v1/meals/{lunch['id']}/leftovers").json()
    assert [m["id"] for m in plan["meals"]] == [dinner["meal_id"]]
    assert meal_in(plan, dinner["meal_id"])["leftover_portions"] == 0
    assert item_amount(plan, "oksekød") == item_amount(before, "oksekød")


def test_leftovers_move_with_their_meal(admin_client):
    dinner = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    admin_client.post(f"/api/v1/meals/{dinner['meal_id']}/leftovers", json={})
    plan = admin_client.post(f"/api/v1/meals/{dinner['meal_id']}/move", json={"date": (NEXT_MONDAY + timedelta(days=2)).isoformat()}).json()
    lunch = next(m for m in plan["meals"] if m["leftover_of"])
    assert lunch["date"] == (NEXT_MONDAY + timedelta(days=3)).isoformat()
    # Leftovers cannot be eaten before the meal is made.
    early = admin_client.post(f"/api/v1/meals/{lunch['id']}/move", json={"date": (NEXT_MONDAY + timedelta(days=1)).isoformat()})
    assert (early.status_code, early.json()["detail"]) == (409, "leftovers_before_meal")


def test_leftovers_have_rules(admin_client):
    dinner = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY + timedelta(days=6))
    # The day after is outside the plan.
    out = admin_client.post(f"/api/v1/meals/{dinner['meal_id']}/leftovers", json={})
    assert (out.status_code, out.json()["detail"]) == (409, "date_not_in_plan")
    before = admin_client.post(f"/api/v1/meals/{dinner['meal_id']}/leftovers", json={"date": (NEXT_MONDAY + timedelta(days=6)).isoformat(), "meal_type": "breakfast"})
    assert before.json()["detail"] == "leftovers_before_meal"
    admin_client.post(f"/api/v1/meals/{dinner['meal_id']}/leftovers", json={"date": (NEXT_MONDAY + timedelta(days=6)).isoformat(), "meal_type": "drink"})
    twice = admin_client.post(f"/api/v1/meals/{dinner['meal_id']}/leftovers", json={"date": (NEXT_MONDAY + timedelta(days=6)).isoformat(), "meal_type": "snack"})
    assert twice.json()["detail"] == "leftovers_planned"


def test_skipping_the_meals_day_takes_its_leftovers_too(admin_client):
    dinner = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    admin_client.post(f"/api/v1/meals/{dinner['meal_id']}/leftovers", json={})
    result = admin_client.patch(f"/api/v1/plans/{dinner['plan_id']}/days/{NEXT_MONDAY.isoformat()}", json={"skipped": True}).json()
    assert result["plan"]["meals"] == []


def test_the_pantry_loses_what_was_cooked_for_leftovers_too(admin_client, db):
    from app.models import PlannedMeal as Row
    from app.services.pantry import consume_meal

    dinner = add(admin_client, "Spaghetti bolognese", NEXT_MONDAY)
    admin_client.post(f"/api/v1/meals/{dinner['meal_id']}/leftovers", json={})
    admin_client.post("/api/v1/pantry", json={"name": "hakket oksekød", "quantity": 2000, "unit": "g"})
    row = db.get(Row, dinner["meal_id"])
    consume_meal(db, row, 0.5)
    db.commit()
    left = next(i for i in admin_client.get("/api/v1/pantry").json() if "oksekød" in i["name"])
    # 500 g for four, cooked for four: two now, two as leftovers.
    assert left["quantity"] == 1500
