"""What the household is about to run out of, before it has.

Two kinds of knowledge go in. Someone said to keep at least a certain amount of something, or
something is bought by hand at a regular pace. Plans take care of ingredients, so what is worth
learning here is everything else that keeps needing buying: coffee, milk, toilet paper.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import local_today
from app.models import Household, PantryItem, ShoppingItem, ShoppingList, User
from app.services.allergens import conflicting_allergy
from app.services.notifications import notify
from app.services.pantry import is_available
from app.services.price_index import active_offers
from app.services.pricing import key_for
from app.services.recipes import household_profile
from app.services.stores import enabled_store_ids
from app.settings_schema import household_settings, user_settings

_HISTORY_DAYS = 365
# Fewer purchases than this is not a habit, and a shorter gap than this is one big shopping week.
_MIN_PURCHASES = 3
_MIN_GAP_DAYS = 3
_REMIND_DAYS_BEFORE = 2
SNOOZE_DAYS = 7


@dataclass
class Purchases:
    name: str
    days: list[date]


def manual_purchases(db: Session, household_id: int, today: date | None = None) -> dict[str, Purchases]:
    """What people put on their lists by hand and then bought, and when they did."""
    since = (today or local_today()) - timedelta(days=_HISTORY_DAYS)
    rows = db.execute(
        select(ShoppingItem.name, ShoppingList.completed_at)
        .join(ShoppingList, ShoppingItem.list_id == ShoppingList.id)
        .where(
            ShoppingList.household_id == household_id,
            ShoppingList.completed_at.is_not(None),
            ShoppingItem.manual.is_(True),
            ShoppingItem.checked.is_(True),
        )
    ).all()
    found: dict[str, Purchases] = {}
    for name, completed_at in rows:
        day = completed_at.date()
        if day < since:
            continue
        found.setdefault(key_for(name), Purchases(name, [])).days.append(day)
    return found


def buying_rhythm(days: list[date]) -> int | None:
    """How many days usually pass between purchases, or None when there is no pattern yet."""
    unique = sorted(set(days))
    if len(unique) < _MIN_PURCHASES:
        return None
    gaps = [(later - earlier).days for earlier, later in zip(unique, unique[1:], strict=False)]
    every = round(median(gaps))
    return every if every >= _MIN_GAP_DAYS else None


def _on_a_list(db: Session, household_id: int) -> set[str]:
    rows = db.scalars(
        select(ShoppingItem.name)
        .join(ShoppingList, ShoppingItem.list_id == ShoppingList.id)
        .where(
            ShoppingList.household_id == household_id,
            ShoppingList.completed_at.is_(None),
            ShoppingItem.covered_by_pantry.is_(False),
        )
    )
    return {key_for(name) for name in rows}


def _is_snoozed(snoozed: dict[str, str], key: str, today: date) -> bool:
    until = snoozed.get(key)
    if not until:
        return False
    try:
        return date.fromisoformat(until) > today
    except ValueError:
        return False


def _cheapest_offer(db: Session, name: str, store_ids: set[int], allergies: list[str]) -> dict | None:
    for offer, store in active_offers(db, name, store_ids, limit=10):
        if conflicting_allergy(f"{offer.title} {offer.search_text}", allergies):
            continue
        return {
            "title": offer.title,
            "store": store.name,
            "price": offer.price,
            "valid_to": offer.valid_to.isoformat() if offer.valid_to else None,
            "url": offer.url,
        }
    return None


def restock_suggestions(
    db: Session, household: Household, user: User | None = None, today: date | None = None, with_offers: bool = True
) -> list[dict]:
    today = today or local_today()
    hs = household_settings(household.settings)
    found: dict[str, dict] = {}

    pantry = list(db.scalars(select(PantryItem).where(PantryItem.household_id == household.id)))
    by_key = {key_for(item.name): item for item in pantry}
    for item in pantry:
        if item.min_quantity is None:
            continue
        have = item.quantity if is_available(item, today) else 0
        if have is None or have >= item.min_quantity:
            continue
        year, week, _ = today.isocalendar()
        found[key_for(item.name)] = {
            "key": key_for(item.name),
            "name": item.name,
            "reason": "below_minimum",
            "quantity": have,
            "unit": item.unit,
            "min_quantity": item.min_quantity,
            # Reminded once a week for as long as it stays low.
            "marker": f"{year}-{week}",
        }

    for key, purchases in manual_purchases(db, household.id, today).items():
        if key in found:
            continue
        every = buying_rhythm(purchases.days)
        if every is None:
            continue
        last = max(purchases.days)
        due = last + timedelta(days=every)
        # Long overdue means they stopped buying it, and nobody wants to be nagged about that.
        if not due - timedelta(days=_REMIND_DAYS_BEFORE) <= today <= due + timedelta(days=max(every, 14)):
            continue
        item = by_key.get(key)
        # Someone has been at the row since the last purchase, and says there is some left.
        if item is not None and item.quantity and item.updated_at.date() > last and is_available(item, today):
            continue
        found[key] = {
            "key": key,
            "name": purchases.name,
            "reason": "usually_bought",
            "every_days": every,
            "last_bought": last.isoformat(),
            "marker": last.isoformat(),
        }

    profile = household_profile(db, household)
    on_a_list = _on_a_list(db, household.id)
    results = [
        suggestion
        for key, suggestion in found.items()
        if key not in on_a_list
        and not _is_snoozed(hs.restock_snoozed, key, today)
        # Whatever else is true of it, nothing goes on a list that someone here must not eat.
        and not conflicting_allergy(suggestion["name"], profile.allergies)
    ]
    if with_offers and results:
        store_ids = enabled_store_ids(db, household, user)
        for suggestion in results:
            suggestion["offer"] = _cheapest_offer(db, suggestion["name"], store_ids, profile.allergies)
    return sorted(results, key=lambda s: s["name"].lower())


def snooze(db: Session, household: Household, key: str, today: date | None = None) -> None:
    hs = household_settings(household.settings)
    hs.restock_snoozed[key] = ((today or local_today()) + timedelta(days=SNOOZE_DAYS)).isoformat()
    household.settings = hs.model_dump(mode="json")
    db.flush()


def run_restock_check(db: Session, today: date | None = None) -> int:
    """Daily job. A given set of things running low is only mentioned once."""
    today = today or local_today()
    sent = 0
    for household in db.scalars(select(Household)):
        suggestions = restock_suggestions(db, household, None, today)
        if not suggestions:
            continue
        markers = sorted(f"{s['key']}:{s['marker']}" for s in suggestions)
        digest = hashlib.sha256("|".join(markers).encode()).hexdigest()[:24]
        data = {
            "items": ", ".join(s["name"] for s in suggestions[:6]),
            "count": len(suggestions),
            "offers": [
                {"name": s["name"], "store": s["offer"]["store"], "price": s["offer"]["price"]}
                for s in suggestions[:6]
                if s.get("offer")
            ],
        }
        for user in db.scalars(select(User).where(User.household_id == household.id, User.is_active.is_(True))):
            if user_settings(user.settings).features.restock and notify(db, user, "restock", data, dedupe_key=f"restock:{digest}"):
                sent += 1
    return sent
