"""The requirement "read ONLY what crawling allows", tested as behaviour."""

from __future__ import annotations

import base64
import html
import json

import httpx
import pytest

from app.crawlers import policy
from app.crawlers.policy import (
    DENY_ALLOWLIST,
    DENY_LEGAL,
    DENY_ROBOTS,
    DENY_ROBOTS_UNREACHABLE,
    CrawlDenied,
    HostRule,
    PoliteClient,
)
from app.crawlers.runner import crawl_source
from app.crawlers.sources import SOURCES, EtilbudsavisSource, TilbudsugenSource, all_source_infos
from app.models import Offer, PricePoint, Store

HOST = "example-offers.dk"
RULES = [HostRule(HOST, ("/offer/",))]


def transport(robots=(200, "User-agent: *\nAllow: /\n", "text/plain"), page=(200, "ok"), log=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(str(request.url))
        if request.url.path == "/robots.txt":
            status, body, content_type = robots
            return httpx.Response(status, text=body, headers={"content-type": content_type})
        status, body = page
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler)


def client(rules=None, **kwargs):
    return PoliteClient(rules or RULES, delay_seconds=1, sleep=lambda _: None, **kwargs)


def test_minetilbud_is_blocked_even_though_it_has_no_robots_txt():
    """The trap: a 404 robots.txt means "allowed" to a robots-only crawler."""
    log: list[str] = []
    rules = [HostRule("minetilbud.dk", ("/",))]
    with client(rules, transport=transport(robots=(404, "", "text/html"), log=log)) as c, pytest.raises(CrawlDenied) as exc:
        c.get("https://minetilbud.dk/katalog/netto/")
    assert exc.value.reason == DENY_LEGAL
    assert log == [], "must not even ask for robots.txt on a legally blocked host"


@pytest.mark.parametrize(
    "url",
    ["https://www.minetilbud.dk/x", "https://publisher.dk/x", "https://api.dayli.dk/x", "https://squid-api.tjek.com/v2/offers"],
)
def test_blocklist_covers_subdomains_and_related_hosts(url):
    assert policy.legal_block_reason(url) is not None


def test_minetilbud_is_not_a_source():
    assert "minetilbud" not in SOURCES
    assert all(info.slug != "minetilbud" for info in all_source_infos())


def test_robots_disallow_is_respected():
    robots = (200, "User-agent: *\nDisallow: /offer/\n", "text/plain")
    with client(transport=transport(robots=robots)) as c, pytest.raises(CrawlDenied) as exc:
        c.get(f"https://{HOST}/offer/kaffe")
    assert exc.value.reason == DENY_ROBOTS


def test_robots_disallow_for_our_user_agent_specifically():
    robots = (200, "User-agent: MadPlanBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n", "text/plain")
    with client(transport=transport(robots=robots)) as c, pytest.raises(CrawlDenied):
        c.get(f"https://{HOST}/offer/kaffe")


def test_missing_robots_allows_when_host_is_not_legally_blocked():
    with client(transport=transport(robots=(404, "", "text/html"))) as c:
        assert c.get(f"https://{HOST}/offer/kaffe").status_code == 200


@pytest.mark.parametrize("status", [500, 503])
def test_unreachable_robots_means_stop(status):
    with client(transport=transport(robots=(status, "", "text/plain"))) as c, pytest.raises(CrawlDenied) as exc:
        c.get(f"https://{HOST}/offer/kaffe")
    assert exc.value.reason == DENY_ROBOTS_UNREACHABLE


@pytest.mark.parametrize("status", [401, 403, 429])
def test_refused_robots_counts_as_no(status):
    with client(transport=transport(robots=(status, "", "text/plain"))) as c, pytest.raises(CrawlDenied):
        c.get(f"https://{HOST}/offer/kaffe")


def test_network_failure_on_robots_means_stop():
    def handler(request):
        raise httpx.ConnectError("down")

    with client(transport=httpx.MockTransport(handler)) as c, pytest.raises(CrawlDenied) as exc:
        c.get(f"https://{HOST}/offer/kaffe")
    assert exc.value.reason == DENY_ROBOTS_UNREACHABLE


def test_html_soft_404_at_robots_is_no_rule_set():
    """tilbudsavis.dk really does answer /robots.txt with its home page."""
    with client(transport=transport(robots=(200, "<!doctype html><html>", "text/html"))) as c:
        assert c.get(f"https://{HOST}/offer/kaffe").status_code == 200


@pytest.mark.parametrize(
    "url",
    [
        f"https://{HOST}/api/private",
        "https://other-site.dk/offer/kaffe",
        f"http://{HOST}/offer/kaffe",
    ],
)
def test_allowlist_rejects_everything_else(url):
    with client(transport=transport()) as c, pytest.raises(CrawlDenied) as exc:
        c.get(url)
    assert exc.value.reason == DENY_ALLOWLIST


def test_redirect_cannot_escape_the_policy():
    """A redirect to a blocked site is checked like any other request, and refused."""
    log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n", headers={"content-type": "text/plain"})
        return httpx.Response(302, headers={"location": "https://minetilbud.dk/katalog/"})

    with client(transport=httpx.MockTransport(handler)) as c, pytest.raises(CrawlDenied) as exc:
        c.get(f"https://{HOST}/offer/kaffe")
    assert exc.value.reason == DENY_LEGAL
    assert not any("minetilbud" in url for url in log)


def test_rate_limit_waits_between_requests_on_one_host():
    now = {"t": 100.0}
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        now["t"] += seconds

    with PoliteClient(RULES, delay_seconds=3, transport=transport(), sleep=sleep, clock=lambda: now["t"]) as c:
        c.get(f"https://{HOST}/offer/a")
        c.get(f"https://{HOST}/offer/b")
    assert slept and all(s >= 3 for s in slept)


def test_crawl_delay_from_robots_wins_when_it_is_longer():
    robots = (200, "User-agent: *\nCrawl-delay: 20\nAllow: /\n", "text/plain")
    now = {"t": 0.0}
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        now["t"] += seconds

    with PoliteClient(RULES, delay_seconds=3, transport=transport(robots=robots), sleep=sleep, clock=lambda: now["t"]) as c:
        c.get(f"https://{HOST}/offer/a")
        c.get(f"https://{HOST}/offer/b")
    assert max(slept) >= 20


def test_delay_can_never_go_below_one_second():
    assert PoliteClient(RULES, delay_seconds=0).delay_seconds == 1.0


def test_adapters_are_limited_to_their_public_pages():
    assert [r.host for r in TilbudsugenSource.info.rules] == ["www.tilbudsugen.dk"]
    assert [r.host for r in EtilbudsavisSource.info.rules] == ["etilbudsavis.dk"]
    for source in SOURCES.values():
        for rule in source.info.rules:
            assert "tjek" not in rule.host and "api" not in rule.host


# Parsers, on synthetic pages that have the real structure.


def tilbudsugen_page(items):
    data = {"props": {"pageProps": {"data": {"pageOfOrganicProductOffers": {"items": items}}, "products": [{"id": 999}]}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def tilbudsugen_item(**overrides):
    item = {
        "countryCode": "DK", "currency": "DKK", "isBorderShop": False, "id": 1001,
        "startDate": "2026-10-30", "endDate": "2026-11-05", "price": "45.00", "units": 1, "unitType": "ps.",
        "quantity": "0.400", "quantityType": "kg.", "quantityUnitCode": "KGM", "pricePerFullUnitOfQuantityType": "112.50",
        "productVariant": {"category": {"name": "Formalet Kaffe"}}, "productName": {"productName": "Kaffe"},
        "brand": {"name": "Merrild"}, "chain": {"name": "REMA 1000"}, "definedDescription": [], "specificNote": [],
    }
    item.update(overrides)
    return item


def test_tilbudsugen_parser_reads_the_real_shape():
    offers = TilbudsugenSource.parse(tilbudsugen_page([tilbudsugen_item()]))
    assert len(offers) == 1
    offer = offers[0]
    assert (offer.chain, offer.price, offer.pack_quantity, offer.pack_unit) == ("REMA 1000", 45.0, 400.0, "g")
    assert (offer.unit_price, offer.unit_basis) == (112.5, "kg")
    assert offer.valid_to.isoformat() == "2026-11-05"
    assert "kaffe" in offer.search_text and "merrild" in offer.search_text
    assert offer.url == "https://www.tilbudsugen.dk/single/1001"


def test_tilbudsugen_parser_skips_border_shops_and_flags_membership():
    offers = TilbudsugenSource.parse(
        tilbudsugen_page(
            [
                tilbudsugen_item(id=1, isBorderShop=True),
                tilbudsugen_item(id=2, countryCode="DE"),
                tilbudsugen_item(id=3, definedDescription=[{"id": 1, "value": "App-Medlemspris"}]),
            ]
        )
    )
    assert [o.external_id for o in offers] == ["3"]
    assert offers[0].membership_required is True


def test_tilbudsugen_parser_survives_a_redesign():
    assert TilbudsugenSource.parse("<html>a new design</html>") == []


def etilbudsavis_page(items, sources=("publication",)):
    key = base64.b64encode(json.dumps(["offers", {"searchTerm": "kaffe", "sources": list(sources)}]).encode()).decode()
    other = base64.b64encode(json.dumps(["auth", None]).encode()).decode()
    body = html.escape(json.dumps({"data": items}))
    return (
        f'<app-data data-key="{other}" data-size="1" data-status="success">{{}}</app-data>'
        f'<app-data data-key="{key}" data-size="9" data-status="success">{body}</app-data>'
    )


def etilbudsavis_item(**overrides):
    item = {
        "publicId": "abc", "name": "BKI", "description": "Formalet kaffe. 150-400 g. Pr. pose", "currencyCode": "DKK",
        "unitPrice": 300, "baseUnit": "kilogram", "price": 45, "savings": None, "membershipPrice": None, "appPrice": None,
        "validFrom": "2026-10-29T23:00:00+0000", "validUntil": "2026-11-05T22:59:59+0000",
        "business": {"name": "SPAR", "countryCode": "DK"},
    }
    item.update(overrides)
    return item


def test_etilbudsavis_parser_converts_dates_to_danish_local_time():
    offers = EtilbudsavisSource.parse(etilbudsavis_page([etilbudsavis_item()]))
    offer = offers[0]
    assert (offer.chain, offer.price, offer.kind) == ("SPAR", 45.0, "offer")
    # 23:00 UTC on the 29th is midnight on the 30th in Denmark.
    assert offer.valid_from.isoformat() == "2026-10-30"
    assert offer.valid_to.isoformat() == "2026-11-05"
    assert (offer.pack_quantity, offer.pack_unit) == (150.0, "g")
    assert (offer.unit_price, offer.unit_basis) == (300.0, "kg")


def test_etilbudsavis_business_products_are_shelf_prices():
    item = etilbudsavis_item(validFrom=None, validUntil=None, name="FORMALET KAFFE", description="400 gram", unitPrice=89.875, price=35.95)
    offer = EtilbudsavisSource.parse(etilbudsavis_page([item], sources=("business_product",)))[0]
    assert offer.kind == "shelf"
    assert offer.valid_to is None


def test_etilbudsavis_savings_give_a_real_before_price():
    offer = EtilbudsavisSource.parse(etilbudsavis_page([etilbudsavis_item(savings=20)]))[0]
    assert offer.pre_price == 65.0


def test_etilbudsavis_ignores_foreign_businesses():
    item = etilbudsavis_item(business={"name": "Citti", "countryCode": "DE"})
    assert EtilbudsavisSource.parse(etilbudsavis_page([item])) == []


# The whole run


class FakeSource:
    def __init__(self, info, pages):
        self.info = info
        self.pages = pages

    def search(self, client, term):
        return TilbudsugenSource.parse(client.get(f"https://{HOST}/offer/{term}").text)


def _source():
    from app.crawlers.policy import SourceInfo

    return FakeSource(SourceInfo("tilbudsugen", "T", f"https://{HOST}", "allowed", "2026-11-03", "", RULES), None)


def test_crawl_stores_offers_history_and_links_stores(db):
    page = tilbudsugen_page([tilbudsugen_item(endDate="2099-01-01")])
    run = crawl_source(db, _source(), ["kaffe"], 1, transport=transport(page=(200, page)), sleep=lambda _: None)
    assert (run.status, run.offers_seen) == ("ok", 1)
    offer = db.query(Offer).one()
    assert db.get(Store, offer.store_id).slug == "rema1000"
    assert db.query(PricePoint).count() == 1


def test_crawl_twice_in_a_day_updates_rather_than_duplicates(db):
    page = tilbudsugen_page([tilbudsugen_item(endDate="2099-01-01")])
    for _ in range(2):
        crawl_source(db, _source(), ["kaffe"], 1, transport=transport(page=(200, page)), sleep=lambda _: None)
    assert db.query(Offer).count() == 1
    assert db.query(PricePoint).count() == 1


def test_crawl_adds_unknown_chains_but_keeps_them_out_of_planning(db):
    page = tilbudsugen_page([tilbudsugen_item(chain={"name": "Matas"}, endDate="2099-01-01")])
    crawl_source(db, _source(), ["creme"], 1, transport=transport(page=(200, page)), sleep=lambda _: None)
    store = db.query(Store).filter_by(slug="matas").one()
    assert store.is_known_grocery is False and store.default_enabled is False


def test_robots_denial_stops_the_run_and_is_recorded(db):
    robots = (200, "User-agent: *\nDisallow: /\n", "text/plain")
    log: list[str] = []
    run = crawl_source(db, _source(), ["a", "b", "c"], 1, transport=transport(robots=robots, log=log), sleep=lambda _: None)
    assert run.status == "denied" and run.denied_by_policy == 1 and run.offers_seen == 0
    assert all(url.endswith("/robots.txt") for url in log), "no pages may be fetched after a denial"


def test_a_bad_page_does_not_lose_the_rest_of_the_run(db):
    good = tilbudsugen_page([tilbudsugen_item(endDate="2099-01-01")])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n", headers={"content-type": "text/plain"})
        if request.url.path.endswith("bad"):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=good)

    run = crawl_source(db, _source(), ["bad", "good"], 1, transport=httpx.MockTransport(handler), sleep=lambda _: None)
    assert (run.status, run.offers_seen) == ("partial", 1)
