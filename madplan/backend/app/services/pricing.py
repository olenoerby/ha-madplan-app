"""Prices an amount of an ingredient from real data, and says where the number came from.

The order is a current offer, then a shelf price, then MadPlan's own estimate. Every normal
price records where it came from: the offer's own "before" price, the median of what stores
charge on the shelf, or the catalog's estimate. A saving against an estimate is reported as
one, so that the figure shown never claims more than is known.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.db import local_today
from app.models import Offer, Store
from app.services.allergens import conflicting_allergy
from app.services.catalog import INGREDIENTS, Ingredient, find_ingredient, is_excluded, significant_words
from app.services.text import WordIndex, lists_alternatives, normalize, product_head, product_name, to_base_unit, words

# Prices more than this far off the estimate are almost always a wrong match ("kaffe" hitting
# a coffee machine) rather than an amazing deal.
_SANITY_LOW = 0.2
_SANITY_HIGH = 5.0
SHELF_MAX_AGE_DAYS = 14


@dataclass
class OfferView:
    id: int
    store_id: int
    store_name: str
    title: str
    search_text: str
    kind: str
    price: float
    pre_price: float | None
    pack_quantity: float | None
    pack_unit: str | None
    unit_price: float | None
    unit_basis: str | None
    membership_required: bool
    valid_to: date | None
    source: str
    url: str | None
    description: str | None = None


@dataclass
class Quote:
    name: str
    quantity: float | None
    unit: str | None
    packs: int
    cost: float | None
    normal_cost: float | None
    price_source: str  # offer | shelf | reference | unknown
    normal_source: str | None = None  # before | shelf | estimate
    store_id: int | None = None
    store_name: str | None = None
    offer_id: int | None = None
    offer_title: str | None = None
    pack_quantity: float | None = None
    membership_required: bool = False

    @property
    def saving(self) -> float:
        if self.cost is None or self.normal_cost is None:
            return 0.0
        return max(0.0, self.normal_cost - self.cost)


def block_key(ingredient_name: str, offer_title: str) -> str:
    """Identifies "this offer is not that ingredient", when someone has told us so."""
    ingredient = find_ingredient(ingredient_name)
    return f"{ingredient.key if ingredient else normalize(ingredient_name)}|{normalize(offer_title)}"


class PriceBook:
    """All usable prices for one household on one shopping day."""

    def __init__(self, offers: list[OfferView], allergies: list[str] | None = None, blocked: list[str] | None = None) -> None:
        allergies = allergies or []
        # An offer is never suggested for purchase if its text names something someone is
        # allergic to, even if the ingredient itself looked safe.
        self.offers = [
            o for o in offers
            if not allergies or not conflicting_allergy(f"{o.title} {o.search_text}", allergies)
        ]
        self._blocked = set(blocked or [])
        self._heads = [product_head(o.title, o.description) for o in self.offers]
        self._names = [product_name(o.title, o.description) for o in self.offers]
        self._index = WordIndex(self._heads)
        self._matches: dict[str, list[OfferView]] = {}
        self._foods: dict[int, set[str]] | None = None

    def _raw_positions(self, ingredient: Ingredient) -> set[int]:
        positions: set[int] = set()
        for term in ingredient.terms:
            positions |= self._index.find(term)
        return {p for p in positions if not is_excluded(ingredient, self._names[p])}

    def _foods_per_offer(self) -> dict[int, set[str]]:
        """Which catalog foods each offer's name mentions. Computed once, for the whole book."""
        if self._foods is None:
            self._foods = {}
            for ingredient in INGREDIENTS:
                # "i olie" says nothing about what the product is, so staples do not count.
                if not ingredient.staple:
                    for position in self._raw_positions(ingredient):
                        self._foods.setdefault(position, set()).add(ingredient.key)
        return self._foods

    def _is_that_food(self, position: int, ingredient: Ingredient) -> bool:
        """False when the name also mentions another food, as in "flødeost hvidløg".

        Which of the two is the product and which the flavour cannot be told from the words, so
        neither gets to claim it. "Koteletter eller flæsk" lists alternatives, and that is fine.
        """
        if lists_alternatives(self._heads[position]):
            return True
        own = set(words(ingredient.key))
        for other in self._foods_per_offer().get(position, ()):
            if other == ingredient.key:
                continue
            other_words = set(words(other))
            # "søde kartofler" contains "kartofler": the offer is the more specific of the two.
            if not other_words < own:
                return False
        return True

    def matches(self, name: str, ingredient: Ingredient | None) -> list[OfferView]:
        key = ingredient.key if ingredient is not None else normalize(name)
        cached = self._matches.get(key)
        if cached is not None:
            return cached
        if ingredient is not None:
            found = [self.offers[p] for p in sorted(self._raw_positions(ingredient)) if self._is_that_food(p, ingredient)]
        else:
            query = significant_words(name)
            found = [self.offers[p] for p in sorted(self._index.find(query))] if query else []
        found = [o for o in found if f"{key}|{normalize(o.title)}" not in self._blocked]
        self._matches[key] = found
        return found

    def current_offers(self, ingredient: Ingredient) -> list[OfferView]:
        """Time-limited offers that really are this food, whether or not the pack size is known."""
        found = self.matches(ingredient.key, ingredient)
        return [o for o in found if o.kind == "offer" and self._plausible(o, ingredient)]

    def _pack_cost(
        self, offer: OfferView, quantity: float, unit: str, ingredient: Ingredient | None
    ) -> tuple[int, float, float | None] | None:
        """(packs to buy, cost, usable pack size), or None if the offer cannot supply this."""
        # A litre of yoghurt and a kilo of yoghurt are close enough for shopping.
        same_kind = offer.pack_unit == unit or {offer.pack_unit, unit} == {"g", "ml"}
        if offer.pack_quantity and same_kind:
            pack = offer.pack_quantity
        elif offer.pack_unit is None or (offer.pack_unit == "stk" and offer.pack_quantity in (None, 1)):
            # Sold per piece or of unknown size. With no size or unit price there is nothing to
            # compare against an amount by weight, so only a piece can be matched to a piece.
            if ingredient is None or ingredient.unit != unit or (unit != "stk" and offer.unit_basis not in ("kg", "l")):
                return None
            pack = ingredient.pack
        elif ingredient is not None and unit == "stk" and offer.pack_unit == "g" and offer.pack_quantity:
            # "3 onions" against a 750 g net, or "1 cabbage" against a price per 500 g.
            if ingredient.piece_g is None:
                return 1, round(offer.price, 2), quantity
            packs = max(1, math.ceil(quantity * ingredient.piece_g / offer.pack_quantity - 1e-9))
            # In pieces, the unit everything else here is counting in.
            return packs, round(packs * offer.price, 2), offer.pack_quantity / ingredient.piece_g
        else:
            return None
        packs = max(1, math.ceil(quantity / pack - 1e-9))
        return packs, round(packs * offer.price, 2), pack

    def _plausible(self, offer: OfferView, ingredient: Ingredient | None) -> bool:
        if ingredient is None:
            return True
        # Vegetables do not come in litres, as anyone looking for squash can find out.
        if ingredient.unit == "stk" and (offer.pack_unit == "ml" or offer.unit_basis == "l"):
            return False
        if not offer.unit_price or not offer.unit_basis:
            return True
        expected = {"g": ("kg", "l"), "ml": ("kg", "l"), "stk": ("stk",)}[ingredient.unit]
        if offer.unit_basis not in expected:
            return True
        return _SANITY_LOW * ingredient.price <= offer.unit_price <= _SANITY_HIGH * ingredient.price

    def per_store(self, name: str, quantity: float | None, unit: str | None) -> dict[int, Quote]:
        """The best quote per store."""
        ingredient = find_ingredient(name)
        base_quantity, base_unit = to_base_unit(quantity, unit)
        if base_quantity is None or base_unit is None:
            base_quantity = ingredient.pack if ingredient else 1
            base_unit = ingredient.unit if ingredient else "stk"

        best: dict[int, Quote] = {}
        rate, normal_source = self.normal_rate(name, base_unit, ingredient)
        for offer in self.matches(name, ingredient):
            if not self._plausible(offer, ingredient):
                continue
            priced = self._pack_cost(offer, base_quantity, base_unit, ingredient)
            if priced is None:
                continue
            packs, cost, pack = priced
            # For what is actually bought. Against a typical pack of twice the size, a 400 g pack
            # of chops looked like a saving of more than it cost.
            if offer.pre_price:
                own_normal = round(packs * offer.pre_price, 2)
            else:
                own_normal = round(rate * packs * pack, 2) if rate is not None and pack else None
            own_source = "before" if offer.pre_price else normal_source
            quote = Quote(
                name=name,
                quantity=base_quantity,
                unit=base_unit,
                packs=packs,
                cost=cost,
                normal_cost=own_normal,
                normal_source=own_source,
                price_source=offer.kind,
                store_id=offer.store_id,
                store_name=offer.store_name,
                offer_id=offer.id,
                offer_title=offer.title,
                pack_quantity=pack,
                membership_required=offer.membership_required,
            )
            current = best.get(offer.store_id)
            if current is None or (quote.cost or 0) < (current.cost or 0):
                best[offer.store_id] = quote
        return best

    def shelf_unit_price(self, name: str, ingredient: Ingredient | None) -> float | None:
        """Median regular price per base-unit amount, from the prices stores charge daily."""
        if ingredient is None:
            return None
        expected = {"g": ("kg", "l"), "ml": ("kg", "l"), "stk": ("stk",)}[ingredient.unit]
        prices = [
            o.unit_price
            for o in self.matches(name, ingredient)
            if o.kind == "shelf" and o.unit_price and o.unit_basis in expected and self._plausible(o, ingredient)
        ]
        # One shop's price is too little to call normal.
        return median(prices) if len(prices) >= 2 else None

    def normal_rate(self, name: str, unit: str, ingredient: Ingredient | None) -> tuple[float | None, str | None]:
        """What one g, ml or piece usually costs, and whether that is measured ("shelf") or our
        "estimate". A litre and a kilo count as the same here, as they do for shopping."""
        if ingredient is None or not (ingredient.unit == unit or {ingredient.unit, unit} == {"g", "ml"}):
            return None, None
        shelf = self.shelf_unit_price(name, ingredient)
        per_unit = shelf or ingredient.price
        return per_unit / (1000 if unit in ("g", "ml") else 1), ("shelf" if shelf else "estimate")

    def normal_cost(self, name: str, quantity: float, unit: str, ingredient: Ingredient | None) -> tuple[float | None, str | None]:
        """What the usual pack or packs of this cost."""
        rate, source = self.normal_rate(name, unit, ingredient)
        if rate is None or ingredient is None:
            return None, None
        packs = max(1, math.ceil(quantity / ingredient.pack - 1e-9))
        return round(rate * ingredient.pack * packs, 2), source

    def quote(
        self, name: str, quantity: float | None, unit: str | None, store_ids: set[int] | None = None
    ) -> Quote:
        """The cheapest way to buy this, limited to some stores if asked."""
        options = self.per_store(name, quantity, unit)
        if store_ids is not None:
            options = {k: v for k, v in options.items() if k in store_ids}
        if options:
            return min(options.values(), key=lambda q: q.cost or 0)
        return self.fallback(name, quantity, unit)

    def fallback(self, name: str, quantity: float | None, unit: str | None) -> Quote:
        """No store has it listed, so use the estimate, or say that the price is unknown."""
        ingredient = find_ingredient(name)
        base_quantity, base_unit = to_base_unit(quantity, unit)
        if ingredient is not None:
            if base_quantity is None or base_unit != ingredient.unit:
                base_quantity, base_unit = ingredient.pack, ingredient.unit
            cost, source = self.normal_cost(name, base_quantity, base_unit, ingredient)
            packs = max(1, math.ceil(base_quantity / ingredient.pack - 1e-9))
            return Quote(
                name=name,
                quantity=base_quantity,
                unit=base_unit,
                packs=packs,
                cost=cost,
                normal_cost=cost,
                normal_source=source,
                price_source="reference",
                pack_quantity=ingredient.pack,
            )
        return Quote(name=name, quantity=base_quantity, unit=base_unit, packs=1, cost=None, normal_cost=None, price_source="unknown")


def load_offer_views(
    db: Session,
    store_ids: set[int],
    on_date: date | None = None,
    window_days: int = 0,
) -> list[OfferView]:
    """Offers valid on the shopping day (or up to ``window_days`` later) in these stores."""
    if not store_ids:
        return []
    day = on_date or local_today()
    shelf_cutoff = local_today() - timedelta(days=SHELF_MAX_AGE_DAYS)
    rows = db.execute(
        select(Offer, Store.name)
        .join(Store, Offer.store_id == Store.id)
        .where(
            Offer.store_id.in_(store_ids),
            or_(
                (Offer.kind == "offer")
                & (or_(Offer.valid_from.is_(None), Offer.valid_from <= day + timedelta(days=window_days)))
                & (or_(Offer.valid_to.is_(None), Offer.valid_to >= day)),
                Offer.kind == "shelf",
            ),
        )
    ).all()
    views = []
    for offer, store_name in rows:
        if offer.kind == "shelf" and offer.last_seen_at.date() < shelf_cutoff:
            continue
        views.append(
            OfferView(
                id=offer.id,
                store_id=offer.store_id,
                store_name=store_name,
                title=offer.title,
                search_text=offer.search_text,
                kind=offer.kind,
                price=offer.price,
                pre_price=offer.pre_price,
                pack_quantity=offer.pack_quantity,
                pack_unit=offer.pack_unit,
                unit_price=offer.unit_price,
                unit_basis=offer.unit_basis,
                membership_required=offer.membership_required,
                valid_to=offer.valid_to,
                source=offer.source,
                url=offer.url,
                description=offer.description,
            )
        )
    return views


def is_staple(name: str) -> bool:
    ingredient = find_ingredient(name)
    return bool(ingredient and ingredient.staple)


def key_for(name: str) -> str:
    ingredient = find_ingredient(name)
    return ingredient.key if ingredient else normalize(name)
