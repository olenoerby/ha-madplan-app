"""Does an ingredient find the right products? The cases come from auditing real offers.

Each of these was seen in real data. Several would have made a meal plan buy the wrong thing,
and cat food would have been the household's "cheapest salmon".
"""

from __future__ import annotations

from datetime import date

import pytest

from app.crawlers.runner import build_terms
from app.crawlers.sources import EtilbudsavisSource
from app.services.catalog import CRAWL_TERMS, find_ingredient
from app.services.pricing import OfferView, PriceBook, block_key
from app.services.text import parse_pack_quantity, product_head, split_ingredient

from .conftest import add_offer, household_of_admin
from .test_crawl_policy import etilbudsavis_item, etilbudsavis_page
from .test_planning import make_plan, set_household


def offer(title, description=None, search_text=None, price=10.0, pack_quantity=500.0, pack_unit="g", unit_price=None, unit_basis=None, kind="offer"):
    return OfferView(
        id=1, store_id=1, store_name="Netto", title=title, search_text=search_text or title.lower(), kind=kind, price=price, pre_price=None,
        pack_quantity=pack_quantity, pack_unit=pack_unit, unit_price=unit_price, unit_basis=unit_basis, membership_required=False, valid_to=None,
        source="test", url=None, description=description,
    )


def matched(ingredient, *offers):
    return [o.title for o in PriceBook(list(offers)).matches(ingredient, find_ingredient(ingredient))]


@pytest.mark.parametrize(
    ("ingredient", "title"),
    [
        ("hakket oksekød", "Rema 1000 Hakket oksekød 15-18%"),
        ("hakket oksekød", "DANISH CROWN Hakket oksekød med 35 % grøntsager"),
        ("kyllingebryst", "Madværket Kyllingebrystfilet"),
        ("laks", "Nautica Laksefilet"),
        ("kartofler", "Freshona Kartofler"),
        ("mælk", "Arla Minimælk 0,4%"),
        ("smør", "Milbona Smør Saltet"),
        ("yoghurt", "Milbona Naturel 3,5% Yoghurt"),
        ("flødeost", "Buko Flødeost Naturel"),
        ("skæreost", "Arla Havarti"),
        ("revet ost", "Coop Revet ost Cheddar"),
        ("løg", "ØGO Løg"),
        ("agurk", "Coop Agurk"),
        ("squash", "Squash"),
        ("hakkede tomater", "ØGO Hakkede tomater"),
        ("pizzadej", "Pizzadej"),
        ("koteletter", "Netto dansk flæsk i skiver eller koteletter"),
        ("stegeflæsk", "Netto dansk flæsk i skiver eller koteletter"),
    ],
)
def test_the_right_products_match(ingredient, title):
    assert matched(ingredient, offer(title)) == [title]


@pytest.mark.parametrize(
    ("ingredient", "title", "description"),
    [
        # Not food for people
        ("laks", "KATTEFODER M/LAKS", None),
        ("rejer", "Chrisco Crunchies Kylling & Rejer", "Kattegodbidder"),
        ("løg", "Løgplanter", None),
        ("rødløg", "Grønne Fingre stikløg rødløg", None),
        ("spinat", "Spinat frø", None),
        # Ready-made dishes
        ("laks", "Rahbek Indbagt Laks med Spinat og Ostesauce", None),
        ("spaghetti", "Beauvais Spaghetti Napoli", None),
        ("porrer", "Gestus Farserede Porrer", None),
        ("peberfrugt", "FYLDTE PEBERFRUGTER", None),
        ("kartofler", "Harvest Basket Crinkle Cut Fries", "Kartofler"),
        ("broccoli", "Deluxe Potato Broccoli & Cheese", None),
        # The word is a flavour, not the product
        ("peberfrugt", "Crefee Smøreost Rød Peberfrugt & Chili", None),
        ("peberfrugt", "SR Food Kyllingebryst Peberfrugt", None),
        ("hvidløg", "Buko Flødeost Hvidløg", None),
        ("bananer", "Madværket Pære & Banan Yoghurt 1,4%", None),
        ("bananer", "PÆRE/BANAN", "150 gr. Arla yoghurt økologisk"),
        ("citron", "Cheasy Citron Yoghurt", None),
        ("tomater", "Elvira MSC Tun i Tomat", None),
        ("tomater", "Glyngøre Tun i vand, olie eller tomat", None),
        ("nudler", "NUDLER M/KYLLINGSMAG", None),
        ("spinat", "FETTUCCINE SPINAT", "250 gr pasta di maria"),
        # Comes with the product
        ("løg", "Den Grønne Slagter Rullepølse med Løg & Krydderier", None),
        ("fløde", "MILBONA Madlavningsjævner med fløde", None),
        # Sounds the same
        ("løg", "Løgismose kaffe", None),
        ("ost", "Kost", None),
        ("ærter", "ØGO Kikærter", None),
        ("rejer", "BUKO Reje- eller skinkeost", None),
        ("æbler", "Rynkeby 16 Saftige Æbler", None),
        ("kaffe", "Cocio, Cocio Iskaffe eller Matilde Kakaomælk", None),
        ("spaghetti", "Dansk græskar hokkaido eller spaghetti squash", None),
        # The more specific ingredient gets it
        ("kartofler", "SØDE KARTOFLER", None),
        ("tomater", "ØGO Hakkede tomater", None),
    ],
)
def test_the_wrong_products_do_not(ingredient, title, description):
    assert matched(ingredient, offer(title, description)) == []


def test_squash_the_vegetable_is_not_squash_the_soft_drink():
    """Both are called squash. Only one of them is sold by the litre."""
    book = PriceBook([offer("TUBORG SQUASH", pack_quantity=330.0, pack_unit="ml", unit_price=9.1, unit_basis="l"), offer("Squash", price=7.0, pack_quantity=1.0, pack_unit="stk")])
    assert book.quote("squash", 1, "stk").cost == 7.0


def test_a_category_alone_does_not_make_a_match():
    """Tilbudsugen files bulgur under "Ris". The search text says so, and the name does not."""
    assert matched("ris", offer("Salling ØKO Bulgur", search_text="salling øko bulgur ris")) == []


def test_a_brand_name_title_is_read_with_its_description():
    assert matched("kaffe", offer("BKI", "Formalet eller Instant kaffe. Flere varianter. 150-400 g."))


def test_marketing_text_further_down_does_not_count():
    long_text = "Lækker til familien. Vælg i mellem kamsteg, frikadeller og fiskefilet."
    assert matched("fiskefilet", offer("Lun lørdag", long_text)) == []


def test_abbreviations_do_not_end_the_description():
    assert "yoghurt" in product_head("PÆRE/BANAN", "150 gr. Arla yoghurt økologisk")
    assert product_head("BKI", "Formalet kaffe. Flere varianter") == "bki formalet kaffe"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("pr. 1/2 kg", (500.0, "g")), ("1/2 kg", (500.0, "g")), ("Flæskesteg pr 1/2 kg", (500.0, "g"))],
)
def test_fractions_are_not_read_as_whole_numbers(text, expected):
    assert parse_pack_quantity(text) == expected


def test_a_steak_sold_by_weight_gets_the_size_its_price_is_for():
    """"Pr. 1/2 kg" at 24.95 and 49.90 per kg: the price is for half a kilo, and not for two."""
    item = etilbudsavis_item(name="MADVÆRKET Flæskesteg", description="pr. 1/2 kg. Pr. kg 49,90", price=24.95, unitPrice=49.9)
    parsed = EtilbudsavisSource.parse(etilbudsavis_page([item]))[0]
    assert (parsed.pack_quantity, parsed.pack_unit) == (500.0, "g")


def test_tilbudsugen_price_per_half_kilo_gets_the_right_size():
    """The listing gave 2 kg, while 24.95 at 49.90 per kg is for half a kilo."""
    from app.crawlers.sources import TilbudsugenSource

    from .test_crawl_policy import tilbudsugen_item, tilbudsugen_page

    item = tilbudsugen_item(price="24.95", quantity="2.000", pricePerFullUnitOfQuantityType="49.90")
    assert TilbudsugenSource.parse(tilbudsugen_page([item]))[0].pack_quantity == 500.0


def test_range_in_text_agrees_with_the_unit_price():
    item = etilbudsavis_item(description="Formalet kaffe. 150-400 g. Max. kg pris 300,00", price=45, unitPrice=300)
    assert EtilbudsavisSource.parse(etilbudsavis_page([item]))[0].pack_quantity == 150.0


# Weights against pieces


def test_a_cabbage_priced_per_half_kilo_costs_more_than_one_price():
    book = PriceBook([offer("Hvidkål", price=3.0, pack_quantity=500.0, pack_unit="g", unit_price=6.0, unit_basis="kg")])
    quote = book.quote("hvidkål", 1, "stk")
    assert (quote.packs, quote.cost) == (3, 9.0)


def test_three_onions_still_buy_one_net():
    assert PriceBook([offer("ØGO Løg", price=10.0, pack_quantity=750.0, pack_unit="g")]).quote("løg", 3, "stk").packs == 1


def test_yoghurt_by_the_kilo_works_for_a_recipe_in_millilitres():
    quote = PriceBook([offer("Milbona Yoghurt naturel", price=5.95, pack_quantity=500.0, pack_unit="g", unit_price=11.9, unit_basis="kg")]).quote("yoghurt", 1200, "ml")
    assert (quote.packs, quote.cost) == (3, 17.85)


def test_a_price_without_any_size_cannot_supply_a_weight():
    """A catered "Mørbrad med svampesauce" at 35 kr per portion is no price for 600 g of meat."""
    book = PriceBook([offer("Grisemørbrad", price=35.0, pack_quantity=None, pack_unit=None, unit_price=35.0, unit_basis="stk")])
    assert book.quote("svinemørbrad", 600, "g").price_source == "reference"


# Honest savings


def test_saving_against_the_stores_own_before_price_is_measured(admin_client, db):
    add_offer(db, "netto", "Hakket oksekød 500 g", 30.0, pack_quantity=500, pack_unit="g", unit_price=60, unit_basis="kg", pre_price=55.0)
    set_household(db, likes=["hakket oksekød"])
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    summary = plan["shopping_list"]["summary"]
    mince = next(i for i in plan["shopping_list"]["items"] if i["name"] == "hakket oksekød")
    assert mince["normal_source"] == "before"
    assert summary["savings_measured"] >= 25


def test_a_saving_is_for_what_is_actually_bought():
    """A 400 g pack of chops at 29.95 once showed a saving of 52 kr, measured against a usual pack
    of 800 g. At the estimate of 95 per kg, 400 g normally cost 38, so the saving is about 8."""
    book = PriceBook([offer("Madværket Nakkekoteletter", price=29.95, pack_quantity=400.0, pack_unit="g", unit_price=74.9, unit_basis="kg")])
    quote = book.quote("koteletter", 400, "g")
    assert quote.normal_cost == 38.0
    assert quote.saving == pytest.approx(8.05)


def test_a_net_of_onions_is_valued_by_the_onions_in_it():
    book = PriceBook([offer("ØGO Løg", price=10.0, pack_quantity=750.0, pack_unit="g", unit_price=13.3, unit_basis="kg")])
    quote = book.quote("løg", 3, "stk")
    assert quote.normal_cost == pytest.approx(750 / 120 * 2)  # 6.25 onions at the estimate of 2 kr


def test_saving_against_shelf_prices_is_measured(admin_client, db):
    add_offer(db, "netto", "Hakket oksekød 500 g", 30.0, pack_quantity=500, pack_unit="g", unit_price=60, unit_basis="kg")
    for store, price in (("meny", 110.0), ("lidl", 120.0)):
        add_offer(db, store, "Hakket oksekød", price * 0.5, kind="shelf", pack_quantity=500, pack_unit="g", unit_price=price, unit_basis="kg", valid_from=None, valid_to=None)
    set_household(db, likes=["hakket oksekød"])
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    mince = next(i for i in plan["shopping_list"]["items"] if i["name"] == "hakket oksekød")
    assert mince["normal_source"] == "shelf"
    assert mince["saving"] > 0


def test_saving_against_our_own_estimate_is_labelled_as_such(admin_client, db):
    """The figure the first version showed as fact. It may be shown, as long as it says what it is."""
    add_offer(db, "netto", "Hakket oksekød 500 g", 30.0, pack_quantity=500, pack_unit="g", unit_price=60, unit_basis="kg")
    set_household(db, likes=["hakket oksekød"])
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    summary = plan["shopping_list"]["summary"]
    mince = next(i for i in plan["shopping_list"]["items"] if i["name"] == "hakket oksekød")
    assert mince["normal_source"] == "estimate"
    assert summary["savings"] > 0
    assert summary["savings_measured"] == 0


def test_one_shops_price_is_too_little_to_call_normal(admin_client, db):
    add_offer(db, "meny", "Hakket oksekød", 55.0, kind="shelf", pack_quantity=500, pack_unit="g", unit_price=110, unit_basis="kg", valid_from=None, valid_to=None)
    book = PriceBook([offer("Hakket oksekød", kind="shelf", unit_price=110, unit_basis="kg")])
    assert book.shelf_unit_price("hakket oksekød", find_ingredient("hakket oksekød")) is None


# "Wrong item"


def test_wrong_item_is_remembered_and_the_list_is_redone(admin_client, db):
    add_offer(db, "netto", "Rare Kartofler Med Noget", 6.0, pack_quantity=2000, pack_unit="g", unit_price=3.0, unit_basis="kg", search_text="rare kartofler")
    add_offer(db, "netto", "Gode Kartofler", 20.0, pack_quantity=2000, pack_unit="g", unit_price=10, unit_basis="kg")
    set_household(db, likes=["kartofler"], repeat_avoid_days=0)
    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    item = next(i for i in plan["shopping_list"]["items"] if i["name"] == "kartofler")
    assert item["offer_title"] == "Rare Kartofler Med Noget"

    fixed = admin_client.post(f"/api/v1/shopping-items/{item['id']}/wrong-match").json()
    item = next(i for i in fixed["items"] if i["name"] == "kartofler")
    assert item["offer_title"] == "Gode Kartofler"

    # And the next plan knows it too.
    again = make_plan(admin_client, mode="days_ahead", days_ahead=1, seed=2)
    titles = [i["offer_title"] for i in again["shopping_list"]["items"]]
    assert "Rare Kartofler Med Noget" not in titles


def test_wrong_item_only_blocks_that_ingredient():
    key = block_key("kartofler", "Mærkelig Vare Med Løg")
    assert key.startswith("kartofler|")
    book = PriceBook([offer("Kartofler")], blocked=[block_key("kartofler", "Kartofler")])
    assert book.matches("kartofler", find_ingredient("kartofler")) == []
    assert matched("kartofler", offer("Kartofler")) == ["Kartofler"]


def test_wrong_item_stays_within_the_household(admin_client, db):
    from app.models import Household

    from .conftest import login_as, make_user

    plan = make_plan(admin_client, mode="days_ahead", days_ahead=1)
    other = Household(name="Naboen", settings={})
    db.add(other)
    db.commit()
    make_user(db, "nabo", household_id=other.id)
    item = plan["shopping_list"]["items"][0]
    assert login_as("nabo").post(f"/api/v1/shopping-items/{item['id']}/wrong-match").status_code == 404


# Recipes imported from the web


def test_a_link_to_another_dish_is_not_usable():
    """Seen for real: a link found for lasagne gave a Turkish lentil soup."""
    from app.services.mealie_sync import is_usable_import

    soup = {"name": "Linse suppe (tyrkisk)", "recipeIngredient": [{"note": "2 dl røde linser"}, {"note": "1 løg"}], "recipeInstructions": [{"text": "Kog det."}]}
    assert is_usable_import(soup, "Lasagne") is False
    assert is_usable_import({**soup, "name": "Hjemmelavet klassisk lasagne | Løgismose"}, "Lasagne") is True
    assert is_usable_import({**soup, "name": "Karrykylling"}, "Kylling i karry med ris") is True
    assert is_usable_import({**soup, "name": "Chokoladekage"}, "Kylling i karry med ris") is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("400 g hakket oksekød", ("hakket oksekød", 400.0, "g")),
        ("2 løg, finthakket", ("løg", 2.0, "stk")),
        ("4 fed hvidløg, finthakket", ("hvidløg", 4.0, "fed")),
        ("2 tsk tørret oregano", ("tørret oregano", 2.0, "tsk")),
        ("2 dl. røde linser", ("røde linser", 2.0, "dl")),
        ("1,5 l mælk", ("mælk", 1.5, "l")),
        ("½ dl fløde", ("fløde", 0.5, "dl")),
        ("1/2 tsk salt", ("salt", 0.5, "tsk")),
        ("2-3 gulerødder", ("gulerødder", 3.0, "stk")),
        ("2 spiseskefulde olie", ("olie", 2.0, "spsk")),
        ("olivenolie", ("olivenolie", None, None)),
        ("salt og peber", ("salt og peber", None, None)),
        ("lidt persille, hakket", ("lidt persille", None, None)),
        ("løg", ("løg", None, None)),
        ("gulerødder, skrællede", ("gulerødder", None, None)),
    ],
)
def test_plain_text_ingredients_are_understood(text, expected):
    assert split_ingredient(text) == expected


def test_a_scraped_mealie_recipe_can_be_priced():
    """What Mealie really holds after scraping a page: no amounts, only the text."""
    from app.integrations.mealie import parse_mealie_ingredient

    raw = {"quantity": 0, "unit": None, "food": None, "note": "400 g hakket oksekød", "display": "400 g hakket oksekød"}
    assert parse_mealie_ingredient(raw) == {"name": "hakket oksekød", "quantity": 400.0, "unit": "g"}
    book = PriceBook([offer("Rema 1000 Hakket oksekød", price=35.0, pack_quantity=400.0, pack_unit="g", unit_price=87.5, unit_basis="kg")])
    parsed = parse_mealie_ingredient(raw)
    assert book.quote(parsed["name"], parsed["quantity"], parsed["unit"]).cost == 35.0


def test_placeholder_recipes_are_not_usable():
    from app.services.mealie_sync import is_usable_import

    placeholder = {"recipeIngredient": [{"note": "Could not detect ingredients"}], "recipeInstructions": [{"text": "Could not detect instructions"}]}
    good = {"recipeIngredient": [{"note": "500 g oksekød"}, {"note": "1 løg"}], "recipeInstructions": [{"text": "Brun kødet."}]}
    assert is_usable_import(placeholder) is False
    assert is_usable_import({"recipeIngredient": good["recipeIngredient"], "recipeInstructions": []}) is False
    assert is_usable_import(good) is True


# Crawling covers it all


def test_default_limit_covers_every_catalog_term(db):
    """Coffee, of all things, once fell off the end of a list that was too short."""
    from app.settings_schema import ScheduleSettings

    terms = build_terms(db, ScheduleSettings().crawl_max_terms)
    assert "kaffe" in terms
    assert set(CRAWL_TERMS) <= set(terms)


def test_a_low_limit_takes_turns_through_the_catalog(db):
    seen: set[str] = set()
    for offset in range(len(CRAWL_TERMS)):
        seen |= set(build_terms(db, 10, date.fromordinal(730000 + offset)))
    assert seen == set(CRAWL_TERMS)


def test_watched_items_always_come_first(admin_client, db):
    admin_client.post("/api/v1/alerts", json={"query": "mascara", "max_price": 100})
    assert build_terms(db, 3)[0] == "mascara"
    assert household_of_admin(db) is not None


def test_alerts_ignore_what_merely_comes_with_a_product(admin_client, db):
    add_offer(db, "netto", "Kattefoder m/laks", 5.0, search_text="kattefoder m laks")
    assert admin_client.post("/api/v1/alerts", json={"query": "laks", "max_price": 20}).json()["last_match"] is None
