"""The meal plan requirements from the specification, tested through the API."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.db import local_today
from app.models import Household, HouseholdStore, MealPlan, PantryItem, Store
from app.services.allergens import conflicting_allergy
from app.services.planner import resolve_dates
from app.services.pricing import OfferView, PriceBook, load_offer_views
from app.services.recipes import ingredient_names
from app.services.shopping import Line, optimize_stores
from app.services.stores import household_store_choices
from app.services.text import parse_pack_quantity, text_matches, word_matches
from app.settings_schema import household_settings

from .conftest import add_offer, household_of_admin, login_as, make_user

NEXT_MONDAY = local_today() + timedelta(days=7 - local_today().weekday())


def set_household(db, **settings):
    household = household_of_admin(db)
    household.settings = {**(household.settings or {}), **settings}
    db.commit()
    return household


def make_plan(client, **overrides):
    body = {"mode": "full_week", "use_ai": False, "seed": 1, **overrides}
    response = client.post("/api/v1/plans", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def dinners(plan):
    return [m for m in plan["meals"] if m["meal_type"] == "dinner"]


# Periods


def test_full_week_is_monday_to_sunday():
    dates = resolve_dates("full_week", today=date(2026, 11, 3))
    assert dates[0] == date(2026, 11, 9) and dates[0].weekday() == 0
    assert dates[-1] == date(2026, 11, 15) and len(dates) == 7


def test_workweek_is_monday_to_friday():
    dates = resolve_dates("workweek", today=date(2026, 11, 3))
    assert [d.weekday() for d in dates] == [0, 1, 2, 3, 4]


def test_x_days_ahead_starts_today():
    dates = resolve_dates("days_ahead", today=date(2026, 11, 3), days_ahead=3)
    assert dates == [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]


def test_range_validates():
    with pytest.raises(ValueError):
        resolve_dates("range", start=date(2026, 11, 5), end=date(2026, 11, 1))
    with pytest.raises(ValueError):
        resolve_dates("range", start=date(2026, 1, 1), end=date(2026, 6, 1))


# Generation


def test_plan_works_without_ai_or_any_offers(admin_client):
    plan = make_plan(admin_client)
    assert len(dinners(plan)) == 7
    assert plan["ai_used"] is False
    assert plan["shopping_list"]["items"]


def test_default_people_come_from_the_household(admin_client):
    plan = make_plan(admin_client)
    assert all((m["adults"], m["children"]) == (2, 0) for m in plan["meals"])


def test_household_defaults_can_change(admin_client, db):
    set_household(db, default_adults=3, default_children=2)
    plan = make_plan(admin_client)
    assert all((m["adults"], m["children"]) == (3, 2) for m in plan["meals"])


def test_all_five_meal_types_can_be_planned(admin_client):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=2, meal_types=["breakfast", "lunch", "dinner", "snack", "drink"])
    assert {m["meal_type"] for m in plan["meals"]} == {"breakfast", "lunch", "dinner", "snack", "drink"}


def test_no_dish_twice_in_one_plan(admin_client):
    titles = [m["title"] for m in dinners(make_plan(admin_client))]
    assert len(titles) == len(set(titles))


def test_skipped_date_gets_no_meals(admin_client):
    skip = NEXT_MONDAY + timedelta(days=2)
    plan = make_plan(admin_client, skip_dates=[skip.isoformat()])
    assert skip.isoformat() not in {m["date"] for m in plan["meals"]}
    assert next(d for d in plan["days"] if d["date"] == skip.isoformat())["skipped"] is True


def test_household_can_always_skip_a_weekday(admin_client, db):
    set_household(db, skip_weekdays=[4])  # Fridays
    plan = make_plan(admin_client)
    assert all(date.fromisoformat(m["date"]).weekday() != 4 for m in plan["meals"])


def test_skip_and_unskip_a_day_afterwards(admin_client):
    plan = make_plan(admin_client)
    day = plan["meals"][0]["date"]
    response = admin_client.patch(f"/api/v1/plans/{plan['id']}/days/{day}", json={"skipped": True})
    assert day not in {m["date"] for m in response.json()["plan"]["meals"]}
    response = admin_client.patch(f"/api/v1/plans/{plan['id']}/days/{day}", json={"skipped": False})
    assert day in {m["date"] for m in response.json()["plan"]["meals"]}


# Allergies are absolute, dislikes are not


def all_ingredients(db, plan_id) -> list[str]:
    plan = db.get(MealPlan, plan_id)
    return [name for meal in plan.meals for name in ingredient_names(meal.recipe)]


def test_allergy_is_absolute(admin_client, db):
    set_household(db, allergies=["nødder"])
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=14, meal_types=["dinner", "snack", "breakfast", "lunch"])
    db.expire_all()
    for name in all_ingredients(db, plan["id"]):
        assert conflicting_allergy(name, ["nødder"]) is None, name
    assert not any("nødder" in m["title"].lower() for m in plan["meals"])


def test_one_members_allergy_protects_the_whole_household(admin_client, db):
    make_user(db, "allergiker", settings={"allergies": ["fisk"]})
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=20)
    db.expire_all()
    for name in all_ingredients(db, plan["id"]):
        assert conflicting_allergy(name, ["fisk"]) is None, name


def test_allergen_offer_is_never_bought(admin_client, db):
    """The dish is safe, and the ingredient is too. This one product of it is not."""
    add_offer(db, "netto", "Rugbrød med valnødder", 5.0, pack_quantity=500, pack_unit="g", unit_price=10, unit_basis="kg")
    add_offer(db, "netto", "Rugbrød med solsikke", 15.0, pack_quantity=500, pack_unit="g", unit_price=30, unit_basis="kg")

    def bought():
        plan = make_plan(admin_client, mode="days_ahead", days_ahead=1, meal_types=["lunch"])
        admin_client.post(f"/api/v1/meals/{plan['meals'][0]['id']}/replace", json={"recipe_id": rye_bread_recipe["id"]})
        items = admin_client.post(f"/api/v1/plans/{plan['id']}/refresh-list").json()["items"]
        return next(i["offer_title"] for i in items if i["name"] == "rugbrød")

    rye_bread_recipe = next(r for r in admin_client.get("/api/v1/recipes", params={"q": "Rugbrød med pålæg"}).json())
    assert bought() == "Rugbrød med valnødder", "the cheapest, as long as nobody is allergic"
    set_household(db, allergies=["nødder"])
    assert bought() == "Rugbrød med solsikke"


def test_buying_an_allergen_by_hand_warns(admin_client, db):
    set_household(db, allergies=["nødder"])
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=2)
    response = admin_client.post(f"/api/v1/shopping-lists/{plan['shopping_list']['id']}/items", json={"name": "Cashewnødder"})
    assert response.json()["allergy_warning"] == "nødder"


def test_cannot_swap_in_a_recipe_with_an_allergen(admin_client, db):
    set_household(db, allergies=["fisk"])
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    recipes = admin_client.get("/api/v1/recipes", params={"q": "laks"}).json()
    salmon = next(r for r in recipes if r["allergy_conflict"])
    response = admin_client.post(f"/api/v1/meals/{plan['meals'][0]['id']}/replace", json={"recipe_id": salmon["id"]})
    assert response.status_code == 409


@pytest.mark.parametrize(
    ("text", "allergy", "expected"),
    [
        ("Hasselnødder", "nødder", True),
        ("Mandler", "nødder", True),
        ("Pesto", "nødder", True),
        ("Nougat", "nødder", True),
        ("Mayonnaise", "æg", True),
        ("Skiveskåret pålæg", "æg", False),  # "æg" inside "pålæg" is not an egg
        ("Krug champagne", "gluten", False),
        ("Hvedemel", "gluten", True),
        ("Rugbrød", "gluten", True),
        ("Fiskefilet", "fisk", True),
        ("Kokosmælk", "nødder", False),
        ("nuts", "nødder", False),
        ("cashew", "nuts", True),
    ],
)
def test_allergen_matching(text, allergy, expected):
    assert (conflicting_allergy(text, [allergy]) is not None) is expected


def test_dislike_ranks_lower_but_can_still_appear(admin_client, db):
    # A budget that never comes into it, so this is about the ranking alone.
    set_household(db, dislikes=["kylling"], budget_weekly=100000)
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=4)
    assert not any("kylling" in m["title"].lower() for m in dinners(plan)), "kept out while there are better options"

    plan = make_plan(admin_client, mode="days_ahead", days_ahead=30)
    assert any("kylling" in m["title"].lower() for m in dinners(plan)), "still suggested before repeating dishes"


def test_budget_swaps_avoid_what_people_dislike(admin_client, db):
    set_household(db, likes=["laks", "flæskesteg", "torsk"], dislikes=["æg"], budget_weekly=100000)
    unlimited = make_plan(admin_client, mode="days_ahead", days_ahead=7)["shopping_list"]["summary"]["total"]
    limited = make_plan(admin_client, mode="days_ahead", days_ahead=7, budget=unlimited * 0.8)
    swapped_in = [m["title"] for m in limited["meals"] if any(r["code"] == "budget_swap" for r in m["reasons"])]
    assert swapped_in
    assert not any(any(r["code"] == "disliked" for r in m["reasons"]) for m in limited["meals"] if m["title"] in swapped_in)


def test_dislike_of_a_meat_covers_its_cuts(admin_client, db):
    """The specification's own example. "Grisekød" has to mean bacon and chops too."""
    set_household(db, dislikes=["grisekød"])
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=4)
    db.expire_all()
    for meal in db.get(MealPlan, plan["id"]).meals:
        text = " ".join(ingredient_names(meal.recipe))
        assert not any(cut in text for cut in ("bacon", "koteletter", "svinekød", "medister", "flæsk", "pølser")), meal.title


def test_like_and_meat_preference_rank_higher(admin_client, db):
    set_household(db, likes=["pita"])
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=2)
    assert any("pita" in m["title"].lower() for m in dinners(plan))
    set_household(db, likes=[], meat_preferences=["torsk"])
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=2)
    assert any("torsk" in m["title"].lower() for m in dinners(plan))


# Diets


def test_vegetarian_excludes_meat_and_fish(admin_client, db):
    set_household(db, diet_mode="vegetarian")
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=5)
    db.expire_all()
    for meal in db.get(MealPlan, plan["id"]).meals:
        assert not any(word in " ".join(ingredient_names(meal.recipe)) for word in ("oksekød", "kylling", "laks", "bacon")), meal.title


def test_keto_avoids_starches(admin_client, db):
    set_household(db, diet_mode="keto")
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=2)
    db.expire_all()
    for meal in db.get(MealPlan, plan["id"]).meals:
        text = " ".join(ingredient_names(meal.recipe))
        assert not any(word in text for word in ("kartofler", "ris", "pasta", "spaghetti")), meal.title


# Repetition


def test_recent_dish_is_avoided_across_plans(admin_client, db):
    """No Lasagne week after week. Past plans hold the dishes served recently."""
    first = make_plan(admin_client, mode="range", start=(local_today() - timedelta(days=7)).isoformat(), end=(local_today() - timedelta(days=1)).isoformat())
    used = {m["title"] for m in first["meals"]}
    second = make_plan(admin_client, mode="days_ahead", days_ahead=7, seed=2)
    assert not used & {m["title"] for m in second["meals"]}


def test_repeat_window_is_a_household_setting(admin_client, db):
    set_household(db, repeat_avoid_days=0)
    first = make_plan(admin_client, mode="range", start=(local_today() - timedelta(days=2)).isoformat(), end=(local_today() - timedelta(days=1)).isoformat(), seed=3)
    second = make_plan(admin_client, mode="days_ahead", days_ahead=2, seed=3)
    assert {m["title"] for m in first["meals"]} & {m["title"] for m in second["meals"]}


def test_same_protein_is_limited_per_week(admin_client, db):
    set_household(db, repeat_protein_per_week=2)
    plan = make_plan(admin_client)
    db.expire_all()
    from collections import Counter

    from app.services.recipes import recipe_traits

    counts: Counter = Counter()
    for meal in db.get(MealPlan, plan["id"]).meals:
        counts.update(recipe_traits(meal.recipe).proteins)
    assert max(counts.values()) <= 3


# Prices and savings


def test_cost_comes_from_real_offers_and_says_so(admin_client, db):
    add_offer(db, "netto", "Hakket oksekød 500 g", 30.0, pack_quantity=500, pack_unit="g", unit_price=60, unit_basis="kg", pre_price=55.0)
    set_household(db, likes=["hakket oksekød"])
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    mince = next(i for i in plan["shopping_list"]["items"] if i["name"] == "hakket oksekød")
    assert mince["store"] == "Netto"
    assert mince["price_source"] == "offer"
    assert mince["cost"] == 30.0
    assert mince["saving"] == 25.0


def test_savings_are_never_a_made_up_percentage(admin_client):
    """No offers at all means estimated prices and therefore nothing saved."""
    plan = make_plan(admin_client)
    summary = plan["shopping_list"]["summary"]
    assert summary["savings"] == 0
    assert summary["total"] > 0
    assert summary["real_prices"] == 0 and summary["estimated_prices"] > 0


def test_list_says_how_many_prices_are_estimates(admin_client, db):
    add_offer(db, "netto", "Kartofler 2 kg", 15.0, pack_quantity=2000, pack_unit="g", unit_price=7.5, unit_basis="kg")
    plan = make_plan(admin_client)
    summary = plan["shopping_list"]["summary"]
    assert summary["real_prices"] >= 1
    assert summary["real_prices"] + summary["estimated_prices"] + summary["unknown_prices"] == sum(
        1 for i in plan["shopping_list"]["items"] if not i["covered_by_pantry"]
    )


def test_offer_in_a_store_not_chosen_is_not_used(admin_client, db):
    add_offer(db, "bilka", "Hakket oksekød 500 g", 5.0, pack_quantity=500, pack_unit="g", unit_price=10, unit_basis="kg")
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=5)
    stores = {i["store"] for i in plan["shopping_list"]["items"]}
    assert "Bilka" not in stores


def test_enabling_a_store_brings_its_offers_in(admin_client, db):
    bilka = db.scalar(select(Store).where(Store.slug == "bilka"))
    assert admin_client.put(f"/api/v1/household/stores/{bilka.id}", json={"enabled": True}).status_code == 200
    add_offer(db, "bilka", "Hakket oksekød 500 g", 20.0, pack_quantity=500, pack_unit="g", unit_price=40, unit_basis="kg")
    set_household(db, likes=["hakket oksekød"])
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    mince = next(i for i in plan["shopping_list"]["items"] if i["name"] == "hakket oksekød")
    assert mince["store"] == "Bilka"


def test_offers_are_matched_on_the_shopping_day(admin_client, db):
    """An offer ending before the shopping day must not count."""
    add_offer(db, "netto", "Hakket oksekød 500 g", 30.0, pack_quantity=500, pack_unit="g", valid_to=local_today() - timedelta(days=1))
    set_household(db, likes=["hakket oksekød"])
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    mince = next(i for i in plan["shopping_list"]["items"] if i["name"] == "hakket oksekød")
    assert mince["price_source"] == "reference"


def test_quantities_add_up_before_packs_are_counted(admin_client, db):
    """Two meals sharing one pack buy one pack."""
    add_offer(db, "netto", "Kartofler 2 kg", 15.0, pack_quantity=2000, pack_unit="g")
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=2)
    meals_with_potato = [m for m in plan["meals"] if m["recipe_id"]]
    assert meals_with_potato
    items = [i for i in plan["shopping_list"]["items"] if i["name"] == "kartofler"]
    assert len(items) <= 1


def offer_view(**kwargs):
    defaults = dict(
        id=1, store_id=1, store_name="Netto", title="Løg 1 kg", search_text="løg 1 kg", kind="offer", price=10.0,
        pre_price=None, pack_quantity=1000.0, pack_unit="g", unit_price=10.0, unit_basis="kg",
        membership_required=False, valid_to=None, source="test", url=None,
    )
    defaults.update(kwargs)
    return OfferView(**defaults)


def test_three_onions_buy_one_bag_not_three():
    quote = PriceBook([offer_view()]).quote("løg", 3, "stk")
    assert (quote.packs, quote.cost) == (1, 10.0)


def test_grams_round_up_to_whole_packs():
    book = PriceBook([offer_view(title="Hakket oksekød 500 g", search_text="hakket oksekød 500 g", price=40.0, pack_quantity=500.0, unit_price=80.0)])
    assert book.quote("hakket oksekød", 1200, "g").packs == 3
    assert book.quote("hakket oksekød", 1200, "g").cost == 120.0


def test_implausible_price_is_treated_as_a_wrong_match():
    """"Kaffe" should not match a coffee machine, and a wild unit price gives it away."""
    book = PriceBook([offer_view(title="Kaffe guld", search_text="kaffe guld", price=2.0, pack_quantity=400.0, unit_price=5.0)])
    assert book.quote("kaffe", 400, "g").price_source == "reference"


@pytest.mark.parametrize(
    ("query", "text", "expected"),
    [
        ("løg", "løgismose kaffe", False),
        ("mælk", "letmælk", True),
        ("mælk", "minimælk 0,4 %", True),
        ("kaffe", "kaffen", True),
        ("smør", "smørbar", False),
        ("ost", "kost", False),
        ("oksekød", "hakket oksekød", True),
        ("coca cola", "coca cola dåser", True),
        ("kaffe", "te", False),
        ("kartofler", "kartoffelchips", False),
    ],
)
def test_danish_compound_matching(query, text, expected):
    assert text_matches(query, text) is expected


def test_word_match_handles_inflection():
    assert word_matches("tomat", "tomater") and word_matches("tomater", "tomat")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("400 g", (400.0, "g")), ("1,5 l", (1500.0, "ml")), ("2 x 125 g", (250.0, "g")),
        ("150-400 g", (150.0, "g")), ("Kaffe 1 kg", (1000.0, "g")), ("Kan ikke læses", (None, None)),
    ],
)
def test_pack_quantity_parsing(text, expected):
    assert parse_pack_quantity(text) == expected


# Budget


def test_plan_fits_the_budget_when_it_can(admin_client, db):
    set_household(db, likes=["laks", "flæskesteg", "torsk"])
    unlimited = make_plan(admin_client, mode="days_ahead", days_ahead=7, budget=100000)["shopping_list"]["summary"]["total"]
    limited = make_plan(admin_client, mode="days_ahead", days_ahead=7, budget=unlimited * 0.8)
    summary = limited["shopping_list"]["summary"]
    assert summary["total"] <= unlimited * 0.8
    assert summary["within_budget"] is True
    assert any(r["code"] == "budget_swap" for m in limited["meals"] for r in m["reasons"])


def test_impossible_budget_is_reported_not_hidden(admin_client):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=7, budget=1)
    assert plan["shopping_list"]["summary"]["within_budget"] is False


# Guests and people per meal


def test_guests_on_a_day_increase_the_food(admin_client):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=2)
    day = plan["meals"][0]["date"]
    before = plan["shopping_list"]["summary"]["total"]
    cost_before = plan["meals"][0]["estimated_cost"]
    response = admin_client.patch(f"/api/v1/plans/{plan['id']}/days/{day}", json={"guest_adults": 4, "guest_children": 2})
    updated = response.json()["plan"]
    meal = next(m for m in updated["meals"] if m["date"] == day)
    assert (meal["adults"], meal["children"]) == (6, 2)
    assert meal["estimated_cost"] > cost_before
    assert updated["shopping_list"]["summary"]["total"] > before
    assert next(d for d in updated["days"] if d["date"] == day)["guest_adults"] == 4


def test_many_guests_can_suggest_something_cheaper(admin_client):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1, seed=4)
    day = plan["meals"][0]["date"]
    response = admin_client.patch(f"/api/v1/plans/{plan['id']}/days/{day}", json={"guest_adults": 20})
    suggestions = response.json()["suggestions"]
    assert isinstance(suggestions, list)
    for suggestion in suggestions:
        assert all(a["cost"] < suggestion["new_cost"] for a in suggestion["alternatives"])


def test_people_can_be_set_per_meal(admin_client):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=2)
    meal = plan["meals"][0]
    response = admin_client.patch(f"/api/v1/meals/{meal['id']}/people", json={"adults": 1, "children": 3})
    updated = next(m for m in response.json()["meals"] if m["id"] == meal["id"])
    assert (updated["adults"], updated["children"]) == (1, 3)
    other = next(m for m in response.json()["meals"] if m["id"] != meal["id"])
    assert (other["adults"], other["children"]) == (2, 0)


def test_children_count_as_smaller_portions(admin_client):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1, adults=2, children=0)
    base = plan["meals"][0]["estimated_cost"]
    meal = plan["meals"][0]["id"]
    with_kids = next(m for m in admin_client.patch(f"/api/v1/meals/{meal}/people", json={"adults": 2, "children": 2}).json()["meals"] if m["id"] == meal)
    with_adults = next(m for m in admin_client.patch(f"/api/v1/meals/{meal}/people", json={"adults": 4, "children": 0}).json()["meals"] if m["id"] == meal)
    assert base < with_kids["estimated_cost"] < with_adults["estimated_cost"]


# Pantry


def test_pantry_items_leave_the_shopping_list(admin_client, db):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=3)
    name = next(i["name"] for i in plan["shopping_list"]["items"] if not i["covered_by_pantry"])
    admin_client.post("/api/v1/pantry", json={"name": name})
    refreshed = admin_client.post(f"/api/v1/plans/{plan['id']}/refresh-list").json()
    assert next(i for i in refreshed["items"] if i["name"] == name)["covered_by_pantry"] is True


def test_partly_stocked_pantry_item_buys_the_rest(admin_client, db):
    set_household(db, likes=["hakket oksekød"])
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    item = next(i for i in plan["shopping_list"]["items"] if i["name"] == "hakket oksekød")
    admin_client.post("/api/v1/pantry", json={"name": "hakket oksekød", "quantity": item["quantity"] / 2, "unit": "g"})
    refreshed = admin_client.post(f"/api/v1/plans/{plan['id']}/refresh-list").json()
    rows = [i for i in refreshed["items"] if i["name"] == "hakket oksekød"]
    assert {i["covered_by_pantry"] for i in rows} == {True, False}


def test_expiring_food_is_used_first(admin_client, db):
    household = household_of_admin(db)
    db.add(PantryItem(household_id=household.id, name="rejer", quantity=300, unit="g", expires_on=local_today() + timedelta(days=1)))
    db.commit()
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=2)
    meal = next(m for m in plan["meals"] if any(r["code"] == "uses_expiring" for r in m["reasons"]))
    assert "reje" in meal["title"].lower()


def test_staples_are_not_on_the_list_by_default(admin_client, db):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=7)
    salt_like = [i for i in plan["shopping_list"]["items"] if i["name"] in ("olie", "oregano", "mel") and not i["covered_by_pantry"]]
    assert salt_like == []


def test_ticks_and_manual_items_survive_a_refresh(admin_client, db):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=2)
    shopping = plan["shopping_list"]
    item = next(i for i in shopping["items"] if not i["covered_by_pantry"])
    admin_client.patch(f"/api/v1/shopping-items/{item['id']}", json={"checked": True})
    admin_client.post(f"/api/v1/shopping-lists/{shopping['id']}/items", json={"name": "Toiletpapir"})
    refreshed = admin_client.post(f"/api/v1/plans/{plan['id']}/refresh-list").json()
    assert any(i["name"] == "Toiletpapir" and i["manual"] for i in refreshed["items"])
    assert next(i for i in refreshed["items"] if i["name"] == item["name"])["checked"] is True


# Choosing stores


def _set_distance(db, slug, km):
    store = db.scalar(select(Store).where(Store.slug == slug))
    household = household_of_admin(db)
    link = db.get(HouseholdStore, (household.id, store.id)) or HouseholdStore(household_id=household.id, store_id=store.id, enabled=True)
    link.distance_km = km
    db.merge(link)
    db.commit()


def _store_scenario(db, netto_km, lidl_km, **settings):
    """Netto has the cheap mince and Lidl the cheap potatoes, so both are tempting."""
    add_offer(db, "netto", "Hakket oksekød 500 g", 30.0, pack_quantity=500, pack_unit="g", unit_price=60, unit_basis="kg")
    add_offer(db, "netto", "Kartofler 2 kg", 40.0, pack_quantity=2000, pack_unit="g", unit_price=20, unit_basis="kg")
    add_offer(db, "lidl", "Hakket oksekød 500 g", 45.0, pack_quantity=500, pack_unit="g", unit_price=90, unit_basis="kg")
    add_offer(db, "lidl", "Kartofler 2 kg", 12.0, pack_quantity=2000, pack_unit="g", unit_price=6, unit_basis="kg")
    _set_distance(db, "netto", netto_km)
    _set_distance(db, "lidl", lidl_km)
    household = set_household(db, **settings)
    db.expire_all()
    choices = household_store_choices(db, household)
    book = PriceBook(load_offer_views(db, {c.store.id for c in choices if c.enabled}))
    lines = [Line("hakket oksekød", 500, "g"), Line("kartofler", 2000, "g")]
    best, options, _ = optimize_stores(lines, book, choices, household_settings(household.settings))
    names = {c.store.id: c.store.name for c in choices}
    return {names[s] for s in best.store_ids}, best, options


def test_two_stores_when_the_second_stop_pays_off(db):
    stores, best, _ = _store_scenario(db, 1.0, 1.0, cost_per_km=1.0, cost_per_extra_store=0)
    assert stores == {"Netto", "Lidl"}
    assert best.goods == 42.0  # 30 for mince at Netto, 12 for potatoes at Lidl


def test_distant_store_is_not_worth_the_drive(db):
    stores, best, _ = _store_scenario(db, 1.0, 40.0, cost_per_km=3.0, cost_per_extra_store=0)
    assert stores == {"Netto"}
    assert best.travel == 6.0


def test_a_small_saving_does_not_justify_an_extra_stop(db):
    """Two nearby stores, but the saving is less than the household values its time at."""
    stores, _, _ = _store_scenario(db, 1.0, 1.0, cost_per_km=1.0, cost_per_extra_store=50)
    assert len(stores) == 1


def test_max_stores_is_respected(db):
    _, _, options = _store_scenario(db, 1.0, 1.0, max_stores=1, cost_per_km=1.0, cost_per_extra_store=0)
    assert all(len(o.store_ids) == 1 for o in options)


def test_unknown_distance_is_flagged_rather_than_assumed_zero(db):
    add_offer(db, "netto", "Kartofler 2 kg", 15.0, pack_quantity=2000, pack_unit="g")
    household = household_of_admin(db)
    choices = household_store_choices(db, household)
    book = PriceBook(load_offer_views(db, {c.store.id for c in choices if c.enabled}))
    best, _, _ = optimize_stores([Line("kartofler", 2000, "g")], book, choices, household_settings(household.settings))
    assert best.missing_distance is True


def test_store_plan_appears_on_the_shopping_list(admin_client, db):
    add_offer(db, "netto", "Hakket oksekød 500 g", 30.0, pack_quantity=500, pack_unit="g", unit_price=60, unit_basis="kg")
    set_household(db, likes=["hakket oksekød"])
    summary = make_plan(admin_client, mode="days_ahead", days_ahead=1)["shopping_list"]["summary"]
    assert summary["store_plan"]["stores"] == ["Netto"]
    assert summary["by_store"][0]["store"] == "Netto"


# Voting


def test_single_rule_decides_on_one_vote(admin_client):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=2)
    meal = plan["meals"][0]["id"]
    result = admin_client.post(f"/api/v1/meals/{meal}/vote", json={"vote": "yes"}).json()
    assert next(m for m in result["meals"] if m["id"] == meal)["status"] == "accepted"
    other = plan["meals"][1]["id"]
    result = admin_client.post(f"/api/v1/meals/{other}/vote", json={"vote": "no"}).json()
    assert next(m for m in result["meals"] if m["id"] == other)["status"] == "rejected"


def test_rejected_meal_leaves_the_shopping_list(admin_client, db):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    before = plan["shopping_list"]["summary"]["total"]
    assert before > 0
    result = admin_client.post(f"/api/v1/meals/{plan['meals'][0]['id']}/vote", json={"vote": "no"}).json()
    assert result["shopping_list"]["summary"]["total"] == 0


def test_changing_your_mind_brings_it_back(admin_client):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    meal = plan["meals"][0]["id"]
    before = plan["shopping_list"]["summary"]["total"]
    admin_client.post(f"/api/v1/meals/{meal}/vote", json={"vote": "no"})
    result = admin_client.post(f"/api/v1/meals/{meal}/vote", json={"vote": "yes"}).json()
    assert result["shopping_list"]["summary"]["total"] == before
    assert next(m for m in result["meals"] if m["id"] == meal)["status"] == "accepted"


def test_family_majority_vote(admin_client, db):
    make_user(db, "mor")
    make_user(db, "far")
    set_household(db, voting_rule="majority")
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    meal = plan["meals"][0]["id"]
    assert plan["meals"][0]["voters"] == 3

    result = admin_client.post(f"/api/v1/meals/{meal}/vote", json={"vote": "yes"}).json()
    assert next(m for m in result["meals"] if m["id"] == meal)["status"] == "pending"
    result = login_as("mor").post(f"/api/v1/meals/{meal}/vote", json={"vote": "yes", "comment": "Dejligt"}).json()
    decided = next(m for m in result["meals"] if m["id"] == meal)
    assert decided["status"] == "accepted"
    assert {v["name"] for v in decided["votes"]} == {"Admin", "Mor"}
    assert any(v["comment"] == "Dejligt" for v in decided["votes"])


def test_unanimous_rule_any_no_rejects(admin_client, db):
    make_user(db, "mor")
    set_household(db, voting_rule="unanimous")
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    meal = plan["meals"][0]["id"]
    assert next(m for m in admin_client.post(f"/api/v1/meals/{meal}/vote", json={"vote": "yes"}).json()["meals"] if m["id"] == meal)["status"] == "pending"
    assert next(m for m in login_as("mor").post(f"/api/v1/meals/{meal}/vote", json={"vote": "no"}).json()["meals"] if m["id"] == meal)["status"] == "rejected"


def test_user_can_opt_out_of_voting(admin_client, db):
    make_user(db, "udenfor", settings={"features": {"voting": False}})
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    assert plan["meals"][0]["voters"] == 1
    other = login_as("udenfor")
    assert other.post(f"/api/v1/meals/{plan['meals'][0]['id']}/vote", json={"vote": "yes"}).status_code == 403


def test_alternatives_from_family_and_system(admin_client, db):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    meal = plan["meals"][0]
    result = admin_client.post(f"/api/v1/meals/{meal['id']}/alternatives", json={"title": "Mormors suppe"}).json()
    assert next(m for m in result["meals"] if m["id"] == meal["id"])["alternatives"][0]["title"] == "Mormors suppe"

    options = admin_client.get(f"/api/v1/meals/{meal['id']}/options").json()
    assert options and all(o["title"] != meal["title"] for o in options)

    # With no AI configured the "ask AI" button still gives the next best dish.
    result = admin_client.post(f"/api/v1/meals/{meal['id']}/ai-alternative").json()
    sources = {a["source"] for a in next(m for m in result["meals"] if m["id"] == meal["id"])["alternatives"]}
    assert "system" in sources


def test_replacing_a_meal_resets_its_vote(admin_client):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    meal = plan["meals"][0]
    admin_client.post(f"/api/v1/meals/{meal['id']}/vote", json={"vote": "no"})
    option = admin_client.get(f"/api/v1/meals/{meal['id']}/options").json()[0]
    result = admin_client.post(f"/api/v1/meals/{meal['id']}/replace", json={"recipe_id": option["recipe_id"]}).json()
    updated = next(m for m in result["meals"] if m["id"] == meal["id"])
    assert (updated["title"], updated["status"], updated["votes"]) == (option["title"], "pending", [])


def test_learned_taste_from_votes(admin_client, db):
    """"The household often says yes to pita, and no to fried herring"."""
    for _ in range(2):
        plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
        meal = plan["meals"][0]
        pita = next(r for r in admin_client.get("/api/v1/recipes", params={"q": "Pita med"}).json())
        admin_client.post(f"/api/v1/meals/{meal['id']}/replace", json={"recipe_id": pita["id"]})
        admin_client.post(f"/api/v1/meals/{meal['id']}/vote", json={"vote": "yes"})
    taste = admin_client.get("/api/v1/household/taste").json()
    assert any(entry["term"] == "pita" for entry in taste["liked"])
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1, seed=9)
    assert any(r["code"] == "usually_yes" for m in plan["meals"] for r in m["reasons"]) or "pita" in plan["meals"][0]["title"].lower()


# Sharing and isolation between households


def _other_household(db) -> Household:
    other = Household(name="Naboen", settings={})
    db.add(other)
    db.commit()
    return other


def test_other_household_cannot_see_or_touch_a_plan(admin_client, db):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    make_user(db, "nabo", household_id=_other_household(db).id)
    other = login_as("nabo")
    assert other.get(f"/api/v1/plans/{plan['id']}").status_code == 404
    assert other.post(f"/api/v1/meals/{plan['meals'][0]['id']}/vote", json={"vote": "no"}).status_code == 404
    assert other.get(f"/api/v1/shopping-lists/{plan['shopping_list']['id']}").status_code == 404
    item = plan["shopping_list"]["items"][0]
    assert other.patch(f"/api/v1/shopping-items/{item['id']}", json={"checked": True}).status_code == 404


def test_other_household_cannot_touch_the_pantry_or_expenses(admin_client, db):
    item = admin_client.post("/api/v1/pantry", json={"name": "Mel"}).json()
    expense = admin_client.post("/api/v1/expenses", json={"day": "2026-01-01", "amount": 100}).json()
    make_user(db, "nabo", household_id=_other_household(db).id)
    other = login_as("nabo")
    assert other.get("/api/v1/pantry").json() == []
    assert other.delete(f"/api/v1/pantry/{item['id']}").status_code == 404
    assert other.delete(f"/api/v1/expenses/{expense['id']}").status_code == 404
    assert other.get("/api/v1/budget").json()["economy"]["this_year_to_date"] == 0


def test_one_users_alerts_are_private(admin_client, db):
    alert = admin_client.post("/api/v1/alerts", json={"query": "Smør", "max_price": 12}).json()
    make_user(db, "mor")
    other = login_as("mor")
    assert other.get("/api/v1/alerts").json() == []
    assert other.delete(f"/api/v1/alerts/{alert['id']}").status_code == 404


def test_shared_plan_can_be_viewed_and_voted_but_not_changed(admin_client, db):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    friend = make_user(db, "ven", household_id=_other_household(db).id)
    assert admin_client.post(f"/api/v1/plans/{plan['id']}/share", json={"user_ids": [friend.id]}).status_code == 200
    other = login_as("ven")
    assert other.get(f"/api/v1/plans/{plan['id']}").status_code == 200
    assert other.post(f"/api/v1/meals/{plan['meals'][0]['id']}/vote", json={"vote": "yes"}).status_code == 200
    assert other.delete(f"/api/v1/plans/{plan['id']}").status_code == 403
    assert other.patch(f"/api/v1/meals/{plan['meals'][0]['id']}/people", json={"adults": 9, "children": 0}).status_code == 403
    listed = other.get("/api/v1/plans").json()
    assert listed[0]["shared"] is True


def test_view_only_share_cannot_vote(admin_client, db):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    friend = make_user(db, "ven", household_id=_other_household(db).id)
    admin_client.post(f"/api/v1/plans/{plan['id']}/share", json={"user_ids": [friend.id], "can_vote": False})
    assert login_as("ven").post(f"/api/v1/meals/{plan['meals'][0]['id']}/vote", json={"vote": "yes"}).status_code == 403


def test_household_recipe_is_invisible_to_others(admin_client, db):
    from app.models import Recipe

    household = household_of_admin(db)
    secret = Recipe(household_id=household.id, source="ai", title="Hemmelig ret", meal_types=["dinner"], ingredients=[], steps=["x"])
    db.add(secret)
    db.commit()
    make_user(db, "nabo", household_id=_other_household(db).id)
    other = login_as("nabo")
    assert other.get(f"/api/v1/recipes/{secret.id}").status_code == 404
    assert not any(r["title"] == "Hemmelig ret" for r in other.get("/api/v1/recipes").json())


# Dishes from ingredients


def test_what_can_i_make_with_these(admin_client):
    results = admin_client.post("/api/v1/pantry/suggest", json={"ingredients": ["hakket oksekød", "kartofler", "løg"]}).json()
    assert results
    assert results[0]["have"]
    assert all("coverage" in r for r in results)


def test_suggestions_respect_allergies(admin_client, db):
    set_household(db, allergies=["fisk"])
    results = admin_client.post("/api/v1/pantry/suggest", json={"ingredients": ["laks"]}).json()
    assert results == []



def test_the_list_says_where_in_the_store_things_are(admin_client):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=2)
    list_id = plan["shopping_list"]["id"]
    admin_client.post(f"/api/v1/shopping-lists/{list_id}/items", json={"name": "Toiletpapir"})
    items = admin_client.get(f"/api/v1/shopping-lists/{list_id}").json()["items"]
    sections = {i["name"]: i["section"] for i in items}
    assert sections["Toiletpapir"] == "household"
    assert all(section in {"produce", "bakery", "meat", "fish", "dairy", "dry", "frozen", "drinks", "snacks", "household", "other"} for section in sections.values())
    assert any(section in {"meat", "fish", "produce", "dairy", "dry"} for section in sections.values())


# How often: rarely, often, always


def _count(plan, word):
    return sum(1 for m in dinners(plan) if word in m["title"].lower())


def test_always_brings_a_food_up(admin_client, db):
    baseline = make_plan(admin_client, seed=3)
    assert _count(baseline, "laks") == 0
    admin_client.delete(f"/api/v1/plans/{baseline['id']}")
    set_household(db, frequencies={"laks": "always"})
    plan = make_plan(admin_client, seed=3)
    assert _count(plan, "laks") >= 1
    assert any(r["code"] == "wanted_always" for m in plan["meals"] for r in m["reasons"])


def test_a_food_wanted_rarely_comes_once_at_most(admin_client, db):
    set_household(db, repeat_protein_per_week=0)
    baseline = make_plan(admin_client, seed=5)
    assert _count(baseline, "kylling") == 2
    admin_client.delete(f"/api/v1/plans/{baseline['id']}")
    set_household(db, frequencies={"kylling": "rarely"})
    plan = make_plan(admin_client, seed=5)
    assert _count(plan, "kylling") <= 1


def test_each_person_counts_once_in_how_often(admin_client, db):
    from app.services.recipes import household_profile

    make_user(db, "mor", settings={"frequencies": {"fisk": "rarely"}})
    make_user(db, "far", settings={"frequencies": {"Fisk": "always"}})
    profile = household_profile(db, household_of_admin(db))
    # One says rarely (-1), one always (+2): together, a little more than usual.
    assert profile.frequencies == {"fisk": 0.5}


def test_how_often_can_be_saved_by_each_person(admin_client):
    settings = admin_client.get("/api/v1/me/settings").json()
    settings["frequencies"] = {"fisk": "often", "pasta": "rarely"}
    saved = admin_client.put("/api/v1/me/settings", json=settings)
    assert saved.status_code == 200, saved.text
    assert admin_client.get("/api/v1/me/settings").json()["frequencies"] == {"fisk": "often", "pasta": "rarely"}
    settings["frequencies"] = {"fisk": "sometimes"}
    assert admin_client.put("/api/v1/me/settings", json=settings).status_code == 422
