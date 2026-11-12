"""Evaluates price alerts against current offers."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import local_today, utcnow
from app.models import Household, Offer, PriceAlert, Store, TrackedItem, User
from app.services.allergens import conflicting_allergy
from app.services.notifications import notify
from app.services.price_index import active_offers, price_index
from app.services.recipes import household_profile
from app.services.restock import manual_purchases
from app.services.stores import enabled_store_ids
from app.services.text import normalize
from app.settings_schema import user_settings

_USUAL_ITEMS = 15
_USUAL_AFTER_PURCHASES = 3


def offer_value(offer: Offer, basis: str) -> float | None:
    """The number an alert compares. A pack alert uses the price on the shelf edge."""
    if basis == "pack":
        return offer.price
    return offer.unit_price if offer.unit_basis == basis else None


def best_match(db: Session, alert: PriceAlert, my_stores: set[int] | None = None) -> tuple[Offer, Store, float] | None:
    """The cheapest deal under the limit, in the user's own stores if any of them has one.

    Otherwise anywhere at all: the point of an alert for a certain make-up is to hear about it
    wherever it turns up. But nobody needs to be told about butter across the German border
    when the Lidl down the road has it too.
    """
    best: tuple[Offer, Store, float] | None = None
    best_mine: tuple[Offer, Store, float] | None = None
    for offer, store in active_offers(db, alert.query):
        value = offer_value(offer, alert.basis)
        if value is None or value > alert.max_price:
            continue
        if best is None or value < best[2]:
            best = (offer, store, value)
        if my_stores and store.id in my_stores and (best_mine is None or value < best_mine[2]):
            best_mine = (offer, store, value)
    return best_mine or best


def evaluate_alert(db: Session, alert: PriceAlert, user: User) -> bool:
    match = best_match(db, alert, enabled_store_ids(db, user.household, user))
    if match is None:
        return False
    offer, store, value = match
    alert.last_triggered_at = utcnow()
    alert.last_match = {
        "title": offer.title,
        "store": store.name,
        "price": offer.price,
        "value": value,
        "basis": alert.basis,
        "valid_to": offer.valid_to.isoformat() if offer.valid_to else None,
        "url": offer.url,
        "source": offer.source,
    }
    notify(
        db,
        user,
        "price_alert",
        {"query": alert.query, **alert.last_match},
        # Once per offer. A new deal, or the same deal at a lower price, notifies again.
        dedupe_key=f"alert:{alert.id}:{offer.source}:{offer.external_id}:{offer.price}",
    )
    return True


def evaluate_all_alerts(db: Session) -> int:
    triggered = 0
    rows = db.execute(
        select(PriceAlert, User)
        .join(User, PriceAlert.user_id == User.id)
        .where(PriceAlert.active.is_(True), User.is_active.is_(True))
    ).all()
    for alert, user in rows:
        if not user_settings(user.settings).features.price_alerts:
            continue
        if evaluate_alert(db, alert, user):
            triggered += 1
    db.commit()
    return triggered


def _worth_watching(db: Session, user: User) -> list[str]:
    """What this person gave a priority, and what the household keeps buying by hand."""
    queries = {query.strip().lower(): None for query in db.scalars(select(TrackedItem.query).where(TrackedItem.user_id == user.id))}
    habits = sorted(manual_purchases(db, user.household_id).items(), key=lambda item: -len(item[1].days))
    for _, purchases in habits[:_USUAL_ITEMS]:
        if len(purchases.days) >= _USUAL_AFTER_PURCHASES:
            queries.setdefault(purchases.name.strip().lower())
    return list(queries)


def run_buy_signals(db: Session) -> int:
    """Tells people when a price is low enough to act on, by the rules of the price index.

    The index says "buy" only once it has real history to compare with, so a new install stays
    quiet. It also has no idea about allergies, and this is where it has to be told.
    """
    sent = 0
    for user in db.scalars(select(User).where(User.is_active.is_(True))).all():
        if not user_settings(user.settings).features.buy_signals:
            continue
        household = db.get(Household, user.household_id)
        allergies = household_profile(db, household).allergies
        for query in _worth_watching(db, user):
            index = price_index(db, query, user, household)
            title = index["current_title"]
            if index["verdict"] != "buy" or conflicting_allergy(f"{query} {title}", allergies):
                continue
            data = {
                "query": query,
                "title": title,
                "store": index["current_store"],
                "price": index["current_pack_price"],
                "typical_low": index["typical_low"],
                "discount": index["discount"],
                "valid_to": index["current_valid_to"],
                "url": index["current_url"],
            }
            # Once per offer and price. A lower price on the same offer is news again.
            key = f"buy:{normalize(query)}:{normalize(index['current_store'])}:{normalize(title)}:{index['current_pack_price']}"
            if notify(db, user, "buy_now", data, dedupe_key=key[:200]):
                sent += 1
    db.commit()
    return sent


def apply_web_result(db: Session, alert: PriceAlert, user: User, result: dict) -> None:
    """Records a price the AI found on the wider web. Marked as such in the notification."""
    alert.last_triggered_at = utcnow()
    alert.last_match = {**result, "source": "web", "basis": "pack", "value": result.get("price")}
    notify(
        db,
        user,
        "price_alert",
        {"query": alert.query, **alert.last_match},
        dedupe_key=f"alert:{alert.id}:web:{result.get('url')}:{result.get('price')}:{local_today().isoformat()[:7]}",
    )
