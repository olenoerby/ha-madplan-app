"""Price history for a product, and whether now is a good time to buy it."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from statistics import mean

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.db import local_today
from app.models import Offer, PricePoint, Store, TrackedItem, User
from app.services.catalog import find_ingredient, is_excluded
from app.services.stores import enabled_store_ids
from app.services.text import product_head, text_matches, words

HISTORY_DAYS = 365
# Too little history to compare against means "we don't know", not a made-up verdict.
MIN_POINTS = 5
MIN_SPAN_DAYS = 14
# Compared with how low the price usually gets, and that is already somebody's offer.
BASE_BUY_DISCOUNT = 0.15
DISCOUNT_PER_PRIORITY = 0.03


def buy_threshold(priority: int) -> float:
    """How far below normal a price must be. Important items trigger sooner."""
    return BASE_BUY_DISCOUNT - DISCOUNT_PER_PRIORITY * max(-3, min(3, priority))


def _prefilter(column, query: str):
    """A cheap SQL filter on the first letters of each word. Python does the exact match."""
    conditions = [column.contains(word[:4]) for word in words(query)]
    return and_(*conditions) if conditions else column.is_not(None)


def _is_about(query: str, search_text: str) -> bool:
    """Whether a product is that thing, rather than something that merely comes with it.

    Looking for coffee should not give iced coffee in a carton, so what is known about a food
    from auditing real offers counts here too.
    """
    if not text_matches(query, product_head(search_text)):
        return False
    ingredient = find_ingredient(query)
    return ingredient is None or not is_excluded(ingredient, search_text)


def active_offers(
    db: Session,
    query: str,
    store_ids: set[int] | None = None,
    on_date: date | None = None,
    include_shelf: bool = False,
    limit: int = 200,
) -> list[tuple[Offer, Store]]:
    day = on_date or local_today()
    statement = (
        select(Offer, Store)
        .join(Store, Offer.store_id == Store.id)
        .where(
            _prefilter(Offer.search_text, query),
            or_(Offer.valid_from.is_(None), Offer.valid_from <= day),
            or_(Offer.valid_to.is_(None), Offer.valid_to >= day),
        )
    )
    if not include_shelf:
        statement = statement.where(Offer.kind == "offer")
    if store_ids is not None:
        statement = statement.where(Offer.store_id.in_(store_ids))
    rows = [(o, s) for o, s in db.execute(statement.order_by(Offer.price)).all() if _is_about(query, o.search_text)]
    return rows[:limit]


def priority_for(db: Session, user: User | None, query: str) -> int:
    if user is None:
        return 0
    item = db.scalar(select(TrackedItem).where(TrackedItem.user_id == user.id, TrackedItem.query == query.strip().lower()))
    return item.priority if item else 0


def _matching_points(db: Session, query: str, since: date) -> list[PricePoint]:
    rows = db.scalars(select(PricePoint).where(_prefilter(PricePoint.search_text, query), PricePoint.observed_on >= since))
    return [p for p in rows if _is_about(query, p.search_text)]


def _main_basis(points: list[PricePoint]) -> str | None:
    """The unit most of the data is in, so that kilos are not compared with litres."""
    bases = Counter(p.unit_basis for p in points if p.unit_basis and p.unit_price)
    return bases.most_common(1)[0][0] if bases else None


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _weekly_lows(values: list[tuple[date, float]]) -> dict[date, float]:
    lows: dict[date, float] = {}
    for day, value in values:
        monday = _monday(day)
        if monday not in lows or value < lows[monday]:
            lows[monday] = value
    return lows


def price_index(db: Session, query: str, user: User | None = None, household=None) -> dict:
    """Is today's lowest price a good one? Judged against how low it usually gets.

    The plain average is shown, but it cannot decide this. "Kaffe" covers both instant at 300 kr
    per kg and ground coffee at 90, so the cheapest would always look like a bargain against the
    average of them all. The lowest price of each past week compares like with like.
    """
    today = local_today()
    points = _matching_points(db, query, today - timedelta(days=HISTORY_DAYS))
    store_ids = enabled_store_ids(db, household, user) if household is not None else None
    current = active_offers(db, query, store_ids, include_shelf=True)

    basis = _main_basis(points)

    def value_of(price: float, unit_price: float | None, unit_basis: str | None) -> float | None:
        if basis is None:
            return price
        return unit_price if unit_basis == basis else None

    history = [(p.observed_on, v) for p in points if (v := value_of(p.price, p.unit_price, p.unit_basis)) is not None]
    current_values = [
        (value_of(o.price, o.unit_price, o.unit_basis), o, s)
        for o, s in current
        if value_of(o.price, o.unit_price, o.unit_basis) is not None
    ]
    best = min(current_values, key=lambda item: item[0], default=None)

    past = [(day, value) for day, value in history if day < today]
    past_lows = _weekly_lows(past)
    days = sorted(day for day, _ in past)
    span = (days[-1] - days[0]).days if days else 0
    enough_history = len(past) >= MIN_POINTS and span >= MIN_SPAN_DAYS and len(past_lows) >= 2

    values = [value for _, value in history]
    average = round(mean(values), 2) if values else None
    lowest = round(min(values), 2) if values else None
    last_month = [value for day, value in history if day >= today - timedelta(days=30)]
    typical_low = round(mean(past_lows.values()), 2) if past_lows else None
    priority = priority_for(db, user, query)
    threshold = buy_threshold(priority)

    discount = None
    verdict = "unknown"
    if best is not None and typical_low:
        discount = round(1 - best[0] / typical_low, 3)
        if enough_history:
            verdict = "buy" if discount >= threshold else "wait"

    return {
        "query": query,
        "basis": basis or "pack",
        "history_points": len(past),
        "history_days": span,
        "enough_history": enough_history,
        "average": average,
        "typical_low": typical_low,
        "lowest": lowest,
        "lowest_30_days": round(min(last_month), 2) if last_month else None,
        "current_lowest": round(best[0], 2) if best else None,
        "current_pack_price": best[1].price if best else None,
        "current_store": best[2].name if best else None,
        "current_title": best[1].title if best else None,
        "current_kind": best[1].kind if best else None,
        "current_valid_to": best[1].valid_to.isoformat() if best and best[1].valid_to else None,
        "current_url": best[1].url if best else None,
        "discount": discount,
        "priority": priority,
        "buy_threshold": round(threshold, 3),
        "verdict": verdict,
    }


def price_history(db: Session, query: str, days: int = 180) -> list[dict]:
    """The lowest price per week, in one unit, which is what a chart of a price actually wants."""
    points = _matching_points(db, query, local_today() - timedelta(days=days))
    basis = _main_basis(points)
    values = [(p.observed_on, p.unit_price if basis else p.price) for p in points if not basis or (p.unit_basis == basis and p.unit_price)]
    return [{"week": week.isoformat(), "lowest": round(value, 2)} for week, value in sorted(_weekly_lows(values).items())]


def cheapest_week_hint(db: Session, query: str) -> dict | None:
    """When the price has historically been lowest, for "when is coffee cheapest?"."""
    series = price_history(db, query, HISTORY_DAYS)
    if not series:
        return None
    best = min(series, key=lambda row: row["lowest"])
    return {"week": best["week"], "lowest": best["lowest"], "weeks_observed": len(series)}
