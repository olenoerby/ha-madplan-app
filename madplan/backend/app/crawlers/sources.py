"""Adapters that turn the allowed public pages into normalized offers.

They parse the JSON the pages already embed for search engines and for hydration. No
JavaScript runs, nothing logs in, and no private API is called. Images are deliberately
not stored or hotlinked; the UI links back to the source instead.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.crawlers.policy import HostRule, PoliteClient, SourceInfo
from app.services.text import normalize, parse_pack_quantity, to_base_unit

log = logging.getLogger(__name__)

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_APP_DATA = re.compile(r'<app-data data-key="([^"]+)"[^>]*>(.*?)</app-data>', re.S)
_BASIS = {"kilogram": "kg", "kg": "kg", "liter": "l", "l": "l", "piece": "stk", "stk": "stk"}


@dataclass
class ParsedOffer:
    external_id: str
    chain: str
    title: str
    description: str | None
    search_text: str
    kind: str
    price: float
    pre_price: float | None
    pack_quantity: float | None
    pack_unit: str | None
    unit_price: float | None
    unit_basis: str | None
    membership_required: bool
    valid_from: date | None
    valid_to: date | None
    url: str | None


class OfferSource(Protocol):
    info: SourceInfo

    def search(self, client: PoliteClient, term: str) -> list[ParsedOffer]: ...


def _size_from_unit_price(
    price: float, unit_price: float | None, basis: str | None, quantity: float | None, unit: str | None
) -> tuple[float | None, str | None]:
    """Price and unit price give the size the price is for. That beats what the listing states,
    as steaks sold by weight say things like "pr. 1/2 kg" or give a size for the whole steak."""
    if not unit_price or basis not in ("kg", "l"):
        return quantity, unit
    from_price = round(price / unit_price * 1000, 0)
    if quantity is None or abs(quantity - from_price) > from_price * 0.3:
        return from_price, ("g" if basis == "kg" else "ml")
    return quantity, unit


def _number(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


class TilbudsugenSource:
    info = SourceInfo(
        slug="tilbudsugen",
        name="Tilbudsugen",
        homepage="https://www.tilbudsugen.dk",
        status="allowed",
        terms_checked_on="2026-11-03",
        note="robots.txt has no Disallow rules and the published terms contain no ban on automated reading.",
        rules=[HostRule("www.tilbudsugen.dk", ("/offer/",))],
    )

    def search(self, client: PoliteClient, term: str) -> list[ParsedOffer]:
        url = f"https://www.tilbudsugen.dk/offer/{quote(normalize(term).replace(' ', '-'))}"
        return self.parse(client.get(url).text)

    @staticmethod
    def parse(page: str) -> list[ParsedOffer]:
        match = _NEXT_DATA.search(page)
        if not match:
            return []
        props = json.loads(match.group(1)).get("props", {}).get("pageProps", {})
        items = ((props.get("data") or {}).get("pageOfOrganicProductOffers") or {}).get("items") or []
        offers = []
        for item in items:
            offer = TilbudsugenSource._parse_item(item)
            if offer:
                offers.append(offer)
        return offers

    @staticmethod
    def _parse_item(item: dict[str, Any]) -> ParsedOffer | None:
        price = _number(item.get("price"))
        chain = (item.get("chain") or {}).get("name")
        if price is None or not chain or item.get("id") is None:
            return None
        if item.get("countryCode") != "DK" or item.get("isBorderShop"):
            return None

        brand = (item.get("brand") or {}).get("name") or ""
        product = (item.get("productName") or {}).get("productName") or ""
        category = ((item.get("productVariant") or {}).get("category") or {}).get("name") or ""
        notes = [d.get("value", "") for d in (item.get("definedDescription") or [])]
        notes += [d.get("value", "") for d in (item.get("specificNote") or [])]

        units = _number(item.get("units")) or 1
        pack_quantity, pack_unit = to_base_unit(_number(item.get("quantity")), item.get("quantityType"))
        if pack_quantity is not None:
            pack_quantity *= units

        basis_code = {"KGM": "kg", "LTR": "l", "C62": "stk"}.get(item.get("quantityUnitCode") or "")
        unit_price = _number(item.get("pricePerFullUnitOfQuantityType"))
        pack_quantity, pack_unit = _size_from_unit_price(price, unit_price, basis_code, pack_quantity, pack_unit)
        return ParsedOffer(
            external_id=str(item["id"]),
            chain=chain,
            title=f"{brand} {product}".strip() or category,
            description=", ".join(n for n in notes if n) or None,
            search_text=normalize(f"{brand} {product} {category} {' '.join(notes)}"),
            kind="offer",
            price=price,
            pre_price=None,
            pack_quantity=pack_quantity,
            pack_unit=pack_unit,
            unit_price=unit_price,
            unit_basis=basis_code,
            membership_required=any("medlem" in n.lower() for n in notes),
            valid_from=date.fromisoformat(item["startDate"]) if item.get("startDate") else None,
            valid_to=date.fromisoformat(item["endDate"]) if item.get("endDate") else None,
            url=f"https://www.tilbudsugen.dk/single/{item['id']}",
        )


class EtilbudsavisSource:
    info = SourceInfo(
        slug="etilbudsavis",
        name="eTilbudsavis",
        homepage="https://etilbudsavis.dk",
        status="allowed",
        terms_checked_on="2026-11-03",
        note=(
            "robots.txt allows all. Only the public search pages are read. Tjek's API needs "
            "written approval (terms 8.3), so it is never called, and offers are never used "
            "to train models (8.6)."
        ),
        rules=[HostRule("etilbudsavis.dk", ("/soeg/",))],
    )

    def search(self, client: PoliteClient, term: str) -> list[ParsedOffer]:
        url = f"https://etilbudsavis.dk/soeg/{quote(normalize(term))}"
        return self.parse(client.get(url).text, url)

    @staticmethod
    def parse(page: str, url: str | None = None) -> list[ParsedOffer]:
        offers: list[ParsedOffer] = []
        for encoded_key, body in _APP_DATA.findall(page):
            try:
                key = json.loads(base64.b64decode(encoded_key))
            except (ValueError, TypeError):
                continue
            if not (isinstance(key, list) and key and key[0] == "offers" and len(key) > 1):
                continue
            sources = (key[1] or {}).get("sources") or []
            # "publication" is a leaflet deal; "business_product" is the regular shelf price.
            kind = "shelf" if "business_product" in sources else "offer"
            try:
                data = json.loads(html.unescape(body)).get("data") or []
            except ValueError:
                log.warning("eTilbudsavis block could not be decoded")
                continue
            for item in data:
                offer = EtilbudsavisSource._parse_item(item, kind, url)
                if offer:
                    offers.append(offer)
        return offers

    @staticmethod
    def _local_date(value: str | None) -> date | None:
        if not value:
            return None
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
        return moment.astimezone(ZoneInfo(settings.timezone)).date()

    @staticmethod
    def _parse_item(item: dict[str, Any], kind: str, url: str | None) -> ParsedOffer | None:
        price = _number(item.get("price"))
        business = item.get("business") or {}
        if price is None or not business.get("name") or not item.get("publicId"):
            return None
        if business.get("countryCode") not in (None, "DK") or item.get("currencyCode") not in (None, "DKK"):
            return None

        title = (item.get("name") or "").strip()
        description = (item.get("description") or "").strip() or None
        unit_price = _number(item.get("unitPrice"))
        basis = _BASIS.get((item.get("baseUnit") or "").lower())

        pack_quantity, pack_unit = parse_pack_quantity(title)
        if pack_quantity is None:
            pack_quantity, pack_unit = parse_pack_quantity(description)
        pack_quantity, pack_unit = _size_from_unit_price(price, unit_price, basis, pack_quantity, pack_unit)

        savings = _number(item.get("savings"))
        return ParsedOffer(
            external_id=str(item["publicId"]),
            chain=business["name"],
            title=title or (description or ""),
            description=description,
            search_text=normalize(f"{title} {description or ''}"),
            kind=kind,
            price=price,
            pre_price=round(price + savings, 2) if savings else None,
            pack_quantity=pack_quantity,
            pack_unit=pack_unit,
            unit_price=unit_price,
            unit_basis=basis,
            membership_required=_number(item.get("membershipPrice")) is not None
            or _number(item.get("appPrice")) is not None,
            valid_from=EtilbudsavisSource._local_date(item.get("validFrom")),
            valid_to=EtilbudsavisSource._local_date(item.get("validUntil")),
            url=url,
        )


SOURCES: dict[str, OfferSource] = {
    TilbudsugenSource.info.slug: TilbudsugenSource(),
    EtilbudsavisSource.info.slug: EtilbudsavisSource(),
}


def all_source_infos() -> Iterable[SourceInfo]:
    yield from (source.info for source in SOURCES.values())
