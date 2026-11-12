"""In-app notifications, also pushed to Home Assistant when a user has that switched on."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.home_assistant import HomeAssistantError
from app.models import Notification, User
from app.services.app_settings import get_integrations, ha_client
from app.settings_schema import user_settings

log = logging.getLogger(__name__)

_DA = {
    "price_alert": "Tilbudsalarm: {query}",
    "waste": "Brug snart: {items}",
    "buy_now": "Køb nu: {query}",
    "restock": "Køb snart: {items}",
    "plan_ready": "Madplanen er klar",
    "list_changed": "Indkøbslisten har ændret pris",
    "meal_added": "Ny ret på madplanen: {title}",
    "mealie_unsent": "{count} godkendte måltider er ikke sendt til Mealie",
}
_EN = {
    "price_alert": "Price alert: {query}",
    "waste": "Use soon: {items}",
    "buy_now": "Buy now: {query}",
    "restock": "Buy soon: {items}",
    "plan_ready": "The meal plan is ready",
    "list_changed": "The shopping list changed price",
    "meal_added": "New dish on the meal plan: {title}",
    "mealie_unsent": "{count} accepted meals have not been sent to Mealie",
}

# Where each message leads in the app, for the ones that go out to a phone.
_PAGES = {
    "price_alert": "/alerts",
    "buy_now": "/offers",
    "waste": "/pantry",
    "restock": "/pantry",
    "plan_ready": "/plan",
    "list_changed": "/shopping",
    "meal_added": "/plan",
    "mealie_unsent": "/plan",
}


def format_title(kind: str, data: dict, locale: str) -> str:
    template = (_EN if locale == "en" else _DA).get(kind, kind)
    safe = {k: v for k, v in data.items() if isinstance(v, (str, int, float))}
    try:
        return template.format(**safe)
    except KeyError:
        return template


def _money(value, en: bool) -> str:
    if value is None:
        return "?"
    number = float(value)
    text = f"{number:.0f}" if number.is_integer() else f"{number:.2f}"
    return f"DKK {text}" if en else f"{text.replace('.', ',')} kr"


def _by_when(days_left, en: bool) -> str:
    if days_left is None or days_left <= 0:
        return "today" if en else "i dag"
    if days_left == 1:
        return "by tomorrow" if en else "senest i morgen"
    return f"within {days_left} days" if en else f"inden for {days_left} dage"


def format_message(kind: str, data: dict, locale: str) -> str:
    en = locale == "en"
    if kind == "price_alert":
        store, price, title = data.get("store", ""), data.get("price"), data.get("title", "")
        return f"{title} – {price} kr ({store})" if not en else f"{title} – DKK {price} ({store})"
    if kind == "buy_now":
        place = f"{data.get('title', '')} – {_money(data.get('price'), en)} ({data.get('store', '')})"
        share = round((data.get("discount") or 0) * 100)
        return f"{place}. {share}% below its usual low." if en else f"{place}. {share} % under den typiske laveste pris."
    if kind == "waste" and data.get("dish"):
        dish = (data.get("dish_en") or data["dish"]) if en else data["dish"]
        when, items = _by_when(data.get("days_left"), en), data.get("items", "")
        return f"Make {dish} {when}. Use soon: {items}" if en else f"Lav {dish} {when}. Brug snart: {items}"
    if kind == "restock":
        deals = "; ".join(f"{d['name']} {_money(d['price'], en)} ({d['store']})" for d in data.get("offers") or [])
        return f"{format_title(kind, data, locale)}. {deals}" if deals else format_title(kind, data, locale)
    if kind == "plan_ready":
        period = f"{data.get('start', '')} – {data.get('end', '')}"
        total = _money(data.get("total"), en)
        return f"{period}. Expected at the till: {total}. Vote on the meals." if en else f"{period}. Forventet pris: {total}. Stem på måltiderne."
    if kind == "list_changed":
        old, new = _money(data.get("old"), en), _money(data.get("new"), en)
        return f"The offers have changed: {old} is now {new}." if en else f"Tilbuddene har ændret sig: {old} er nu {new}."
    if kind == "mealie_unsent":
        title, first = data.get("title", ""), data.get("first", "")
        if en:
            return f"The first is {title} on {first}. Press \"Save and send to Mealie\" in the meal plan, or send from Home Assistant."
        return f"Den første er {title} {first}. Tryk \"Gem og Send til Mealie\" på madplanen, eller send fra Home Assistant."
    if kind == "meal_added":
        by, title, day = data.get("by", ""), data.get("title", ""), data.get("date", "")
        if en:
            return f"{by} put {title} on {day}. Vote on it in the meal plan."
        return f"{by} har sat {title} på {day}. Stem på den i madplanen."
    return format_title(kind, data, locale)


def page_link(kind: str) -> str | None:
    """A link to the page in the app. Inside Home Assistant the app's address is Home Assistant's
    own, which the app does not know, so messages carry none and people open the app's panel."""
    return None


def notify(db: Session, user: User, kind: str, data: dict, dedupe_key: str | None = None) -> Notification | None:
    """Stores a notification, unless this exact one was already sent."""
    if dedupe_key:
        existing = db.scalar(
            select(Notification.id).where(Notification.user_id == user.id, Notification.dedupe_key == dedupe_key)
        )
        if existing is not None:
            return None
    notification = Notification(user_id=user.id, kind=kind, data=data, dedupe_key=dedupe_key)
    db.add(notification)
    db.flush()
    _push_to_home_assistant(db, user, kind, data)
    return notification


def _push_to_home_assistant(db: Session, user: User, kind: str, data: dict) -> None:
    if not user_settings(user.settings).features.ha_notifications:
        return
    # The person's own phone first, then the default in the app's configuration.
    service = user_settings(user.settings).notify_service or get_integrations(db, user.household_id).ha_notify_service
    if not service:
        return
    client = ha_client(db, user.household_id)
    if client is None:
        return
    message = format_message(kind, data, user.locale)
    link = page_link(kind)
    try:
        with client:
            client.notify(service, format_title(kind, data, user.locale), f"{message}\n{link}" if link else message)
    except HomeAssistantError as exc:
        # The in-app notification is already stored, so a failed push loses nothing.
        log.warning("could not push notification to Home Assistant: %s", exc)
