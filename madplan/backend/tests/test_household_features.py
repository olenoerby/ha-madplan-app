"""Alerts, price index, budget and economy, the waste agent and the diet coach."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.db import local_today
from app.models import Expense, Notification, PantryItem, PricePoint, Store
from app.services.alerts import evaluate_all_alerts
from app.services.household_care import run_waste_check
from app.services.price_index import buy_threshold

from .conftest import add_offer, household_of_admin, login_as, make_user
from .test_planning import make_plan, set_household


def add_history(db, text, store_slug, prices_by_days_ago, unit_basis="kg"):
    store = db.scalar(select(Store).where(Store.slug == store_slug))
    for i, (days_ago, price) in enumerate(prices_by_days_ago):
        db.add(
            PricePoint(
                source="test", external_id=f"h-{text}-{i}", store_id=store.id, search_text=text, kind="offer",
                price=price, unit_price=price, unit_basis=unit_basis, observed_on=local_today() - timedelta(days=days_ago),
            )
        )
    db.commit()


HISTORY = [(120, 55), (90, 56), (60, 54), (30, 55), (14, 55)]


# Price alerts


def test_alert_fires_at_once_when_a_deal_exists(admin_client, db):
    """"Smør under 12 kr"."""
    add_offer(db, "netto", "Lurpak Smør 200 g", 10.5)
    alert = admin_client.post("/api/v1/alerts", json={"query": "Smør", "max_price": 12}).json()
    assert alert["last_match"]["store"] == "Netto"
    assert alert["last_match"]["price"] == 10.5
    notifications = admin_client.get("/api/v1/notifications").json()
    assert notifications[0]["kind"] == "price_alert"


def test_alert_stays_quiet_above_the_limit(admin_client, db):
    add_offer(db, "netto", "Lurpak Smør 200 g", 18)
    assert admin_client.post("/api/v1/alerts", json={"query": "Smør", "max_price": 12}).json()["last_match"] is None
    assert admin_client.get("/api/v1/notifications").json() == []


def test_unit_price_alert(admin_client, db):
    """"Coca cola dåser under 10 kr/liter"."""
    add_offer(db, "netto", "Coca Cola dåser 24 x 33 cl", 89, unit_price=11.2, unit_basis="l")
    add_offer(db, "lidl", "Coca Cola dåser 18 x 33 cl", 55, unit_price=9.25, unit_basis="l")
    alert = admin_client.post("/api/v1/alerts", json={"query": "Coca Cola", "max_price": 10, "basis": "l"}).json()
    assert alert["last_match"]["store"] == "Lidl"
    assert alert["last_match"]["value"] == 9.25


def test_unit_alert_ignores_offers_in_another_unit(admin_client, db):
    add_offer(db, "netto", "Coca Cola 6-pak", 5, unit_price=5, unit_basis="stk")
    assert admin_client.post("/api/v1/alerts", json={"query": "Coca Cola", "max_price": 10, "basis": "l"}).json()["last_match"] is None


def test_alert_fires_once_per_offer(admin_client, db):
    add_offer(db, "netto", "Lurpak Smør", 10.5)
    admin_client.post("/api/v1/alerts", json={"query": "Smør", "max_price": 12})
    evaluate_all_alerts(db)
    evaluate_all_alerts(db)
    assert len(admin_client.get("/api/v1/notifications").json()) == 1


def test_a_new_deal_fires_again(admin_client, db):
    add_offer(db, "netto", "Lurpak Smør", 10.5)
    admin_client.post("/api/v1/alerts", json={"query": "Smør", "max_price": 12})
    add_offer(db, "lidl", "Lurpak Smør", 9.0)
    evaluate_all_alerts(db)
    assert len(admin_client.get("/api/v1/notifications").json()) == 2


def test_alert_prefers_my_own_stores(admin_client, db):
    """Butter across the German border is no use when the Lidl down the road has it too."""
    from app.services.stores import store_for_chain

    store_for_chain(db, "fakta Tyskland", {})
    db.commit()
    add_offer(db, "fakta-tyskland", "Coop Smør", 8.99)
    add_offer(db, "lidl", "Milbona Smør", 9.95)
    match = admin_client.post("/api/v1/alerts", json={"query": "Smør", "max_price": 15}).json()["last_match"]
    assert (match["store"], match["price"]) == ("Lidl", 9.95)


def test_alert_for_coffee_does_not_fire_on_iced_coffee(admin_client, db):
    add_offer(db, "spar", "Cocio, Cocio Iskaffe eller Matilde Kakaomælk", 6.0)
    add_offer(db, "lidl", "Peter Larsen Kaffe", 39.0)
    match = admin_client.post("/api/v1/alerts", json={"query": "kaffe", "max_price": 60}).json()["last_match"]
    assert match["title"] == "Peter Larsen Kaffe"


def test_expired_offer_does_not_fire(admin_client, db):
    add_offer(db, "netto", "Lurpak Smør", 10.5, valid_to=local_today() - timedelta(days=1))
    assert admin_client.post("/api/v1/alerts", json={"query": "Smør", "max_price": 12}).json()["last_match"] is None


def test_alert_finds_deals_in_shops_not_used_for_planning(admin_client, db):
    """The make-up case: look everywhere, not just in the stores the household plans from."""
    from app.services.stores import store_for_chain

    store_for_chain(db, "Matas", {})
    db.commit()
    add_offer(db, "matas", "Maybelline mascara", 49)
    alert = admin_client.post("/api/v1/alerts", json={"query": "mascara", "max_price": 60}).json()
    assert alert["last_match"]["store"] == "Matas"


def test_inactive_alert_and_user_switch_are_respected(admin_client, db):
    alert = admin_client.post("/api/v1/alerts", json={"query": "Smør", "max_price": 12, "active": False}).json()
    add_offer(db, "netto", "Lurpak Smør", 10.5)
    evaluate_all_alerts(db)
    assert admin_client.get("/api/v1/notifications").json() == []

    make_user(db, "mor", settings={"features": {"price_alerts": False}})
    mor = login_as("mor")
    assert mor.post("/api/v1/alerts", json={"query": "Smør", "max_price": 12}).status_code == 403
    assert alert["active"] is False


def test_read_notifications(admin_client, db):
    add_offer(db, "netto", "Lurpak Smør", 10.5)
    admin_client.post("/api/v1/alerts", json={"query": "Smør", "max_price": 12})
    assert len(admin_client.get("/api/v1/notifications", params={"unread_only": True}).json()) == 1
    admin_client.post("/api/v1/notifications/read-all")
    assert admin_client.get("/api/v1/notifications", params={"unread_only": True}).json() == []


# Price index and "buy now"


def test_the_specifications_coffee_example(admin_client, db):
    """"Kaffe, historic average 55 kr, cheapest now 23 kr, BUY NOW: YES"."""
    add_history(db, "kaffe merrild", "netto", HISTORY)
    add_offer(db, "netto", "Merrild kaffe", 23, unit_price=23, unit_basis="kg")
    index = admin_client.get("/api/v1/offers/index", params={"q": "Kaffe"}).json()
    assert index["average"] == 55
    assert index["current_lowest"] == 23
    assert index["current_store"] == "Netto"
    assert index["verdict"] == "buy"


def test_normal_price_says_wait(admin_client, db):
    add_history(db, "kaffe merrild", "netto", HISTORY)
    add_offer(db, "netto", "Merrild kaffe", 52, unit_price=52, unit_basis="kg")
    assert admin_client.get("/api/v1/offers/index", params={"q": "kaffe"}).json()["verdict"] == "wait"


def test_too_little_history_gives_no_verdict(admin_client, db):
    """Never pretend to know: two data points cannot tell whether a price is good."""
    add_history(db, "kaffe merrild", "netto", [(3, 55), (1, 55)])
    add_offer(db, "netto", "Merrild kaffe", 23, unit_price=23, unit_basis="kg")
    index = admin_client.get("/api/v1/offers/index", params={"q": "kaffe"}).json()
    assert index["verdict"] == "unknown"
    assert index["enough_history"] is False


def test_no_data_at_all_is_reported_as_such(admin_client):
    index = admin_client.get("/api/v1/offers/index", params={"q": "kaffe"}).json()
    assert index["verdict"] == "unknown" and index["average"] is None and index["current_lowest"] is None


def test_priority_moves_the_threshold():
    assert buy_threshold(3) < buy_threshold(0) < buy_threshold(-3)
    assert buy_threshold(0) == pytest.approx(0.15)
    assert buy_threshold(99) == buy_threshold(3)


def test_high_priority_buys_sooner_than_low(admin_client, db):
    """Milk matters more than cold cuts: the same discount can mean buy for one, not the other."""
    add_history(db, "kaffe merrild", "netto", HISTORY)
    add_offer(db, "netto", "Merrild kaffe", 48, unit_price=48, unit_basis="kg")  # 13% below usual
    assert admin_client.get("/api/v1/offers/index", params={"q": "kaffe"}).json()["verdict"] == "wait"
    admin_client.put("/api/v1/me/items", json={"query": "kaffe", "priority": 3})
    index = admin_client.get("/api/v1/offers/index", params={"q": "kaffe"}).json()
    assert (index["priority"], index["verdict"]) == (3, "buy")
    admin_client.put("/api/v1/me/items", json={"query": "kaffe", "priority": -3})
    assert admin_client.get("/api/v1/offers/index", params={"q": "kaffe"}).json()["verdict"] == "wait"


def test_the_cheapest_kind_is_not_a_bargain_against_the_average_of_all_kinds(admin_client, db):
    """Seen with real data: instant coffee at 300 per kg lifted the average, so ground coffee at
    its usual 80 per kg would always have said "buy now"."""
    weeks = [(7 * i, 80) for i in range(1, 8)]
    add_history(db, "kaffe formalet", "lidl", weeks)
    add_history(db, "kaffe instant", "meny", [(day + 1, 300) for day, _ in weeks])
    add_offer(db, "lidl", "Formalet kaffe", 40, unit_price=80, unit_basis="kg", pack_quantity=500, pack_unit="g")
    index = admin_client.get("/api/v1/offers/index", params={"q": "kaffe"}).json()
    assert index["average"] > 150
    assert index["typical_low"] == 80
    assert index["current_lowest"] == 80
    assert index["verdict"] == "wait"


def test_price_chart_leaves_out_other_products_and_units(admin_client, db):
    add_history(db, "kaffe formalet", "lidl", [(7, 90), (14, 95), (21, 92)])
    add_history(db, "cocio iskaffe", "spar", [(7, 24)], unit_basis="l")
    add_history(db, "kaffe instant pulver", "spar", [(7, 5)], unit_basis="l")
    lows = [row["lowest"] for row in admin_client.get("/api/v1/offers/index", params={"q": "kaffe"}).json()["history"]]
    assert lows and min(lows) >= 90


def test_priority_is_limited_to_three_steps(admin_client):
    assert admin_client.put("/api/v1/me/items", json={"query": "kaffe", "priority": 4}).status_code == 422
    assert admin_client.put("/api/v1/me/items", json={"query": "kaffe", "priority": -4}).status_code == 422


def test_priorities_are_per_user(admin_client, db):
    admin_client.put("/api/v1/me/items", json={"query": "kaffe", "priority": 3})
    make_user(db, "mor")
    assert login_as("mor").get("/api/v1/offers/index", params={"q": "kaffe"}).json()["priority"] == 0


def test_buy_now_lists_my_tracked_items(admin_client, db):
    add_history(db, "kaffe merrild", "netto", HISTORY)
    add_offer(db, "netto", "Merrild kaffe", 23, unit_price=23, unit_basis="kg")
    admin_client.put("/api/v1/me/items", json={"query": "kaffe", "priority": 1})
    rows = admin_client.get("/api/v1/offers/buy-now").json()
    assert [(r["query"], r["verdict"]) for r in rows] == [("kaffe", "buy")]


def test_price_history_gives_the_low_for_each_week(admin_client, db):
    add_history(db, "kaffe merrild", "netto", [(14, 50), (13, 40), (7, 60)])
    history = admin_client.get("/api/v1/offers/index", params={"q": "kaffe"}).json()["history"]
    assert 40 in [row["lowest"] for row in history]


def test_shelf_price_counts_as_current_but_not_as_a_deal_for_alerts(admin_client, db):
    add_offer(db, "rema1000", "Formalet kaffe", 35.95, kind="shelf", valid_from=None, valid_to=None)
    assert admin_client.get("/api/v1/offers/index", params={"q": "kaffe"}).json()["current_kind"] == "shelf"
    assert admin_client.post("/api/v1/alerts", json={"query": "kaffe", "max_price": 40}).json()["last_match"] is None


def test_search_shows_where_the_price_comes_from(admin_client, db):
    add_offer(db, "netto", "Kaffe BKI", 45, source="etilbudsavis", url="https://etilbudsavis.dk/soeg/kaffe")
    rows = admin_client.get("/api/v1/offers/search", params={"q": "kaffe"}).json()
    assert rows[0]["source"] == "etilbudsavis"
    assert rows[0]["url"].startswith("https://etilbudsavis.dk")


def test_search_can_widen_beyond_my_stores(admin_client, db):
    add_offer(db, "bilka", "Kaffe BKI", 45)
    assert admin_client.get("/api/v1/offers/search", params={"q": "kaffe"}).json() == []
    assert len(admin_client.get("/api/v1/offers/search", params={"q": "kaffe", "all_stores": True}).json()) == 1


# Budget and economy


def test_dashboard_shows_budget_expected_and_savings(admin_client, db):
    set_household(db, budget_weekly=900, budget_monthly=3600)
    add_offer(db, "netto", "Hakket oksekød 500 g", 30.0, pack_quantity=500, pack_unit="g", unit_price=60, unit_basis="kg", pre_price=55.0)
    set_household(db, budget_weekly=900, budget_monthly=3600, likes=["hakket oksekød"])
    make_plan(admin_client, mode="days_ahead", days_ahead=1)
    data = admin_client.get("/api/v1/budget").json()
    assert data["week"]["budget"] == 900 and data["month"]["budget"] == 3600
    assert data["week"]["planned"] > 0
    assert data["week"]["expected"] == data["week"]["spent"] + data["week"]["planned"]
    assert data["week"]["savings"] > 0
    assert data["week"]["remaining"] == round(900 - data["week"]["expected"], 2)


def test_going_over_budget_is_flagged(admin_client, db):
    set_household(db, budget_weekly=10)
    admin_client.post("/api/v1/expenses", json={"day": local_today().isoformat(), "amount": 500})
    assert admin_client.get("/api/v1/budget").json()["week"]["over_budget"] is True


def test_personal_weekly_budget_overrides_the_households(admin_client, db):
    set_household(db, budget_weekly=900)
    settings = admin_client.get("/api/v1/me/settings").json()
    settings["budget_weekly"] = 400
    admin_client.put("/api/v1/me/settings", json=settings)
    assert admin_client.get("/api/v1/budget").json()["week"]["budget"] == 400


def test_economy_compares_with_last_year(admin_client, db):
    """"We spend 28% less on groceries than last year"."""
    household = household_of_admin(db)
    today = local_today()
    last_year = today.replace(year=today.year - 1, day=min(today.day, 28))
    db.add(Expense(household_id=household.id, day=last_year, amount=1000))
    db.add(Expense(household_id=household.id, day=today, amount=720))
    db.commit()
    economy = admin_client.get("/api/v1/budget").json()["economy"]
    assert economy["change_percent"] == -28.0


def test_economy_gives_no_percentage_without_last_years_figures(admin_client, db):
    admin_client.post("/api/v1/expenses", json={"day": local_today().isoformat(), "amount": 720})
    assert admin_client.get("/api/v1/budget").json()["economy"]["change_percent"] is None


def test_finishing_a_trip_records_spending_and_fills_the_pantry(admin_client, db):
    add_offer(db, "netto", "Hakket oksekød 500 g", 30.0, pack_quantity=500, pack_unit="g", unit_price=60, unit_basis="kg", pre_price=55.0)
    set_household(db, likes=["hakket oksekød"])
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    shopping = plan["shopping_list"]
    for item in shopping["items"]:
        if not item["covered_by_pantry"]:
            admin_client.patch(f"/api/v1/shopping-items/{item['id']}", json={"checked": True})
    result = admin_client.post(f"/api/v1/shopping-lists/{shopping['id']}/complete", json={"amount": 199.5}).json()
    assert (result["amount"], result["basis"]) == (199.5, "receipt")
    assert result["savings"] > 0
    assert any(i["name"] == "hakket oksekød" for i in admin_client.get("/api/v1/pantry").json())
    assert admin_client.get("/api/v1/budget").json()["week"]["spent"] == 199.5
    assert admin_client.post(f"/api/v1/shopping-lists/{shopping['id']}/complete", json={}).status_code == 409


def test_a_finished_list_no_longer_counts_as_planned(admin_client, db):
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    shopping = plan["shopping_list"]
    admin_client.post(f"/api/v1/shopping-lists/{shopping['id']}/complete", json={"amount": 100})
    data = admin_client.get("/api/v1/budget").json()["week"]
    assert (data["planned"], data["spent"]) == (0, 100)


# Waste agent


def pantry(db, name, days, **kwargs):
    household = household_of_admin(db)
    item = PantryItem(household_id=household.id, name=name, expires_on=local_today() + timedelta(days=days), **kwargs)
    db.add(item)
    db.commit()
    return item


def test_waste_agent_lists_expiring_food_with_dishes(admin_client, db):
    pantry(db, "hakket kylling", 1)
    pantry(db, "kartofler", 60)
    data = admin_client.get("/api/v1/waste").json()
    assert [i["name"] for i in data["items"]] == ["hakket kylling"]
    assert data["items"][0]["days_left"] == 1
    assert any("hakket kylling" in dish["uses"] for dish in data["dishes"])


def test_waste_suggestions_respect_allergies(admin_client, db):
    pantry(db, "laks", 1)
    set_household(db, allergies=["fisk"])
    assert admin_client.get("/api/v1/waste").json()["dishes"] == []


def test_waste_check_notifies_once_a_day(admin_client, db):
    pantry(db, "hakket kylling", 1)
    assert run_waste_check(db) == 1
    assert run_waste_check(db) == 0
    notification = db.scalar(select(Notification).where(Notification.kind == "waste"))
    assert "hakket kylling" in notification.data["items"]


def test_waste_agent_can_be_switched_off_per_user(admin_client, db):
    settings = admin_client.get("/api/v1/me/settings").json()
    settings["features"]["waste_agent"] = False
    admin_client.put("/api/v1/me/settings", json=settings)
    pantry(db, "hakket kylling", 1)
    assert admin_client.get("/api/v1/waste").status_code == 403
    assert run_waste_check(db) == 0


# Diet coach


def _eat(client, days=4):
    plan = make_plan(client, mode="range", start=(local_today() - timedelta(days=days)).isoformat(), end=local_today().isoformat())
    client.post(f"/api/v1/plans/{plan['id']}/accept-all")
    return plan


def test_diet_report_counts_what_was_actually_eaten(admin_client, db):
    assert admin_client.get("/api/v1/diet/report").json()["meals"] == 0
    _eat(admin_client)
    report = admin_client.get("/api/v1/diet/report").json()
    assert report["meals"] == 5
    assert report["vegetarian_meals"] + report["fish_meals"] + report["red_meat_meals"] > 0


def test_rejected_meals_do_not_count_as_eaten(admin_client, db):
    plan = make_plan(admin_client, mode="range", start=(local_today() - timedelta(days=1)).isoformat(), end=local_today().isoformat())
    admin_client.post(f"/api/v1/meals/{plan['meals'][0]['id']}/vote", json={"vote": "no"})
    admin_client.post(f"/api/v1/meals/{plan['meals'][1]['id']}/vote", json={"vote": "yes"})
    assert admin_client.get("/api/v1/diet/report").json()["meals"] == 1


def test_diet_coach_is_a_setting_at_both_levels(admin_client, db):
    assert admin_client.get("/api/v1/diet/report").json()["coach_enabled"] is False
    assert admin_client.post("/api/v1/diet/advice").status_code == 403
    set_household(db, diet_coach_enabled=True)
    assert admin_client.get("/api/v1/diet/report").json()["coach_enabled"] is True
    settings = admin_client.get("/api/v1/me/settings").json()
    settings["diet_coach_enabled"] = False
    admin_client.put("/api/v1/me/settings", json=settings)
    assert admin_client.get("/api/v1/diet/report").json()["coach_enabled"] is False


# Settings


def test_every_feature_can_be_switched_off_per_user(admin_client, db):
    make_user(db, "mor", settings={"features": {"assistant": False, "pantry_photo": False, "diet_coach": False}})
    mor = login_as("mor")
    assert mor.post("/api/v1/assistant", json={"message": "hej"}).status_code == 403
    assert mor.post("/api/v1/pantry/scan", files={"photos": ("a.jpg", b"x", "image/jpeg")}).status_code == 403
    assert mor.get("/api/v1/diet/report").status_code == 403
    assert admin_client.post("/api/v1/assistant", json={"message": "hej"}).status_code == 200


def test_settings_round_trip_and_stay_separate(admin_client, db):
    make_user(db, "mor")
    mor = login_as("mor")
    settings = mor.get("/api/v1/me/settings").json()
    settings["dislikes"] = ["koriander"]
    settings["features"]["assistant"] = False
    assert mor.put("/api/v1/me/settings", json=settings).status_code == 200
    assert mor.get("/api/v1/me/settings").json()["dislikes"] == ["koriander"]
    assert admin_client.get("/api/v1/me/settings").json()["dislikes"] == []


def test_the_language_follows_home_assistant(admin_client, home_assistant):
    from app.core import ha

    assert admin_client.get("/api/v1/auth/me").json()["locale"] == "da"
    for language, expected in (("en", "en"), ("de", "en"), ("da-DK", "da")):
        home_assistant.language = language
        ha.forget_cached_answers()
        assert admin_client.get("/api/v1/auth/me").json()["locale"] == expected


def test_user_can_override_the_households_stores(admin_client, db):
    bilka = db.scalar(select(Store).where(Store.slug == "bilka"))
    make_user(db, "mor")
    mor = login_as("mor")
    assert mor.put(f"/api/v1/me/stores/{bilka.id}", json={"enabled": True, "favorite": True}).status_code == 200
    row = next(s for s in mor.get("/api/v1/stores").json() if s["slug"] == "bilka")
    assert row["enabled"] is True and row["favorite"] is True
    assert next(s for s in admin_client.get("/api/v1/stores").json() if s["slug"] == "bilka")["enabled"] is False


def test_requested_stores_have_the_right_defaults(admin_client):
    stores = {s["name"]: s["enabled"] for s in admin_client.get("/api/v1/stores").json()}
    for name in ("Netto", "Rema 1000", "Coop 365", "Løvbjerg", "Meny", "Lidl"):
        assert stores[name] is True, name
    for name in ("Bilka", "Føtex", "Spar", "SuperBrugsen", "Kvickly"):
        assert stores[name] is False, name


def test_favorites_and_nearest_sort_first(admin_client, db):
    from app.models import HouseholdStore

    meny = db.scalar(select(Store).where(Store.slug == "meny"))
    lidl = db.scalar(select(Store).where(Store.slug == "lidl"))
    netto = db.scalar(select(Store).where(Store.slug == "netto"))
    household = household_of_admin(db)
    db.add(HouseholdStore(household_id=household.id, store_id=meny.id, enabled=True, favorite=True, distance_km=9))
    db.add(HouseholdStore(household_id=household.id, store_id=lidl.id, enabled=True, distance_km=1))
    db.add(HouseholdStore(household_id=household.id, store_id=netto.id, enabled=True, distance_km=4))
    db.commit()
    names = [s["name"] for s in admin_client.get("/api/v1/stores").json()][:3]
    assert names == ["Meny", "Lidl", "Netto"]


def test_phone_position_only_counts_when_the_user_allowed_it(admin_client, db):
    from app.models import HouseholdStore

    lidl = db.scalar(select(Store).where(Store.slug == "lidl"))
    household = household_of_admin(db)
    db.add(HouseholdStore(household_id=household.id, store_id=lidl.id, enabled=True, lat=55.70, lon=12.50, distance_km=99))
    db.commit()
    params = {"lat": 55.70, "lon": 12.50}
    assert next(s for s in admin_client.get("/api/v1/stores", params=params).json() if s["slug"] == "lidl")["distance_km"] == 99
    settings = admin_client.get("/api/v1/me/settings").json()
    settings["use_phone_location"] = True
    admin_client.put("/api/v1/me/settings", json=settings)
    assert next(s for s in admin_client.get("/api/v1/stores", params=params).json() if s["slug"] == "lidl")["distance_km"] == 0


def nominatim(rows_by_query):
    import httpx

    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        assert "MadPlan" in request.headers["user-agent"], "the policy asks applications to identify themselves"
        return httpx.Response(200, json=rows_by_query.get(request.url.params["q"], []))

    return httpx.MockTransport(handler), calls


def test_nearest_branch_per_chain(db):
    from sqlalchemy import select

    from app.services.geo import nearest_branches

    stores = list(db.scalars(select(Store).where(Store.slug.in_(["netto", "coop365"])).order_by(Store.slug)))
    rows = {
        "Netto": [
            {"category": "shop", "name": "Netto", "display_name": "Netto, Langt Væk", "lat": "55.70", "lon": "12.60"},
            {"category": "shop", "name": "Netto", "display_name": "Netto, Tæt På", "lat": "55.676", "lon": "12.570"},
            {"category": "amenity", "name": "Netto", "display_name": "Ikke en butik", "lat": "55.6756", "lon": "12.5696"},
            {"category": "shop", "name": "Netto-Grillen", "display_name": "Andet", "lat": "55.6756", "lon": "12.5696"},
        ],
        "Coop 365": [{"category": "shop", "name": "365discount", "display_name": "365discount, Her", "lat": "55.68", "lon": "12.58"}],
    }
    transport, calls = nominatim(rows)
    pauses = []
    found = nearest_branches(55.6756, 12.5696, 3, stores, transport=transport, sleep=pauses.append)
    assert found["netto"]["name"] == "Netto, Tæt På"
    assert found["coop365"]["name"].startswith("365discount")
    assert len(calls) == 2 and calls[0]["bounded"] == "1"
    assert pauses and all(p >= 1 for p in pauses), "the policy allows at most one request a second"


def test_address_lookup_needs_the_option_switched_on(admin_client):
    """Sending the home address to a public service must be a deliberate choice."""
    assert admin_client.post("/api/v1/household/geocode", json={"address": "Rådhuspladsen 1, København"}).status_code == 403
    assert admin_client.post("/api/v1/household/stores/nearby").status_code == 403
