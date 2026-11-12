"""Offers, price alerts, the pantry, budget, the assistant, and the waste and diet helpers."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.ai.engine import AiUnavailable
from app.api.deps import CurrentUser, Db, household_of, require_feature
from app.models import ChatMessage, Expense, PantryItem, PriceAlert, TrackedItem
from app.services import proposals
from app.services.alerts import evaluate_alert
from app.services.allergens import conflicting_allergy
from app.services.app_settings import get_ai_settings
from app.services.budget import dashboard
from app.services.household_care import diet_report, waste_suggestions
from app.services.pantry import load_stock
from app.services.planner import recipes_matching_ingredients
from app.services.price_index import active_offers, price_history, price_index
from app.services.recipes import household_profile
from app.services.restock import restock_suggestions, snooze
from app.services.shopping import add_manual_item
from app.services.stores import enabled_store_ids
from app.settings_schema import household_settings, user_settings

router = APIRouter(tags=["household"])

MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_PHOTOS = 6
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}


# Offers


@router.get("/offers/search")
def search_offers(user: CurrentUser, db: Db, q: str, all_stores: bool = False, include_shelf: bool = True) -> list[dict]:
    """``all_stores`` also shows chains not chosen by the household, like when hunting a bargain."""
    if len(q.strip()) < 2:
        return []
    store_ids = None if all_stores else enabled_store_ids(db, household_of(db, user), user)
    return [
        {
            "id": offer.id,
            "title": offer.title,
            "description": offer.description,
            "store": store.name,
            "kind": offer.kind,
            "price": offer.price,
            "pre_price": offer.pre_price,
            "pack_quantity": offer.pack_quantity,
            "pack_unit": offer.pack_unit,
            "unit_price": offer.unit_price,
            "unit_basis": offer.unit_basis,
            "membership_required": offer.membership_required,
            "valid_from": offer.valid_from,
            "valid_to": offer.valid_to,
            "source": offer.source,
            "url": offer.url,
        }
        for offer, store in active_offers(db, q, store_ids, include_shelf=include_shelf, limit=60)
    ]


@router.get("/offers/index")
def get_price_index(user: CurrentUser, db: Db, q: str) -> dict:
    result = price_index(db, q, user, household_of(db, user))
    result["history"] = price_history(db, q)
    return result


@router.get("/offers/buy-now")
def buy_now(user: CurrentUser, db: Db) -> list[dict]:
    """The verdict for everything this user has given a priority."""
    household = household_of(db, user)
    items = db.scalars(select(TrackedItem).where(TrackedItem.user_id == user.id).order_by(TrackedItem.priority.desc()))
    return [price_index(db, item.query, user, household) for item in items]


# Price alerts


class AlertIn(BaseModel):
    query: str = Field(min_length=2, max_length=160)
    max_price: float = Field(gt=0)
    basis: str = Field(default="pack", pattern="^(pack|kg|l|stk)$")
    active: bool = True
    use_web_search: bool = False


def _alert_payload(alert: PriceAlert) -> dict:
    return {
        "id": alert.id,
        "query": alert.query,
        "max_price": alert.max_price,
        "basis": alert.basis,
        "active": alert.active,
        "use_web_search": alert.use_web_search,
        "last_triggered_at": alert.last_triggered_at,
        "last_match": alert.last_match,
    }


def _get_alert(db, alert_id: int, user) -> PriceAlert:
    alert = db.get(PriceAlert, alert_id)
    if alert is None or alert.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    return alert


@router.get("/alerts")
def list_alerts(user: CurrentUser, db: Db) -> list[dict]:
    rows = db.scalars(select(PriceAlert).where(PriceAlert.user_id == user.id).order_by(PriceAlert.id.desc()))
    return [_alert_payload(a) for a in rows]


@router.post("/alerts", status_code=201)
def create_alert(payload: AlertIn, user: CurrentUser, db: Db) -> dict:
    require_feature(user, "price_alerts")
    alert = PriceAlert(user_id=user.id, **payload.model_dump())
    db.add(alert)
    db.flush()
    evaluate_alert(db, alert, user)
    db.commit()
    return _alert_payload(alert)


@router.put("/alerts/{alert_id}")
def update_alert(alert_id: int, payload: AlertIn, user: CurrentUser, db: Db) -> dict:
    alert = _get_alert(db, alert_id, user)
    for field, value in payload.model_dump().items():
        setattr(alert, field, value)
    if alert.active:
        evaluate_alert(db, alert, user)
    db.commit()
    return _alert_payload(alert)


@router.delete("/alerts/{alert_id}")
def delete_alert(alert_id: int, user: CurrentUser, db: Db) -> dict:
    db.delete(_get_alert(db, alert_id, user))
    db.commit()
    return {"ok": True}


# Pantry


class PantryIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    quantity: float | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=12)
    location: str | None = Field(default=None, max_length=60)
    expires_on: date | None = None
    min_quantity: float | None = Field(default=None, ge=0)


def _pantry_payload(item: PantryItem) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "quantity": item.quantity,
        "unit": item.unit,
        "location": item.location,
        "expires_on": item.expires_on,
        "expires_estimated": item.expires_estimated,
        "min_quantity": item.min_quantity,
        "source": item.source,
    }


def _get_pantry_item(db, item_id: int, user) -> PantryItem:
    item = db.get(PantryItem, item_id)
    if item is None or item.household_id != user.household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    return item


@router.get("/pantry")
def list_pantry(user: CurrentUser, db: Db) -> list[dict]:
    rows = db.scalars(select(PantryItem).where(PantryItem.household_id == user.household_id).order_by(PantryItem.name))
    return [_pantry_payload(i) for i in rows]


@router.post("/pantry", status_code=201)
def add_pantry(payload: PantryIn, user: CurrentUser, db: Db) -> dict:
    item = PantryItem(household_id=user.household_id, **payload.model_dump())
    db.add(item)
    db.commit()
    return _pantry_payload(item)


@router.put("/pantry/{item_id}")
def update_pantry(item_id: int, payload: PantryIn, user: CurrentUser, db: Db) -> dict:
    item = _get_pantry_item(db, item_id, user)
    # A date someone typed in or changed is theirs. One left as it was is still MadPlan's guess.
    if payload.expires_on != item.expires_on:
        item.expires_estimated = False
    for field, value in payload.model_dump().items():
        setattr(item, field, value)
    db.commit()
    return _pantry_payload(item)


@router.delete("/pantry/{item_id}")
def delete_pantry(item_id: int, user: CurrentUser, db: Db) -> dict:
    db.delete(_get_pantry_item(db, item_id, user))
    db.commit()
    return {"ok": True}


def _merge_scanned(db, household_id: int, scanned: list) -> list[PantryItem]:
    existing = {i.name.lower(): i for i in db.scalars(select(PantryItem).where(PantryItem.household_id == household_id))}
    touched = []
    for entry in scanned:
        expires = None
        if entry.expires_on:
            try:
                expires = date.fromisoformat(entry.expires_on)
            except ValueError:
                expires = None
        current = existing.get(entry.name.lower())
        if current is None:
            current = PantryItem(household_id=household_id, name=entry.name[:160], quantity=entry.quantity, unit=entry.unit, expires_on=expires, source="photo")
            db.add(current)
            existing[entry.name.lower()] = current
        else:
            if entry.quantity is not None:
                current.quantity, current.unit = entry.quantity, entry.unit
            if expires:
                current.expires_on, current.expires_estimated = expires, False
        touched.append(current)
    db.flush()
    return touched


@router.post("/pantry/scan")
async def scan_pantry(user: CurrentUser, db: Db, photos: list[UploadFile] = File(...), apply: bool | None = Form(default=None)) -> dict:
    """Finds food in photos. The pictures are only held in memory while they are analysed."""
    require_feature(user, "pantry_photo")
    if len(photos) > MAX_PHOTOS:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "too_many_photos")
    images: list[tuple[bytes, str]] = []
    for photo in photos:
        if photo.content_type not in PHOTO_TYPES:
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "unsupported_image_type")
        data = await photo.read(MAX_PHOTO_BYTES + 1)
        if len(data) > MAX_PHOTO_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "photo_too_large")
        images.append((data, photo.content_type))

    from fastapi.concurrency import run_in_threadpool

    from app.ai.features import scan_pantry_photos

    ai = get_ai_settings(db)
    household_id, user_id, locale = user.household_id, user.id, user.locale
    try:
        # A worker thread, as the AI engine runs its own event loop.
        scanned = await run_in_threadpool(scan_pantry_photos, images, household_id, user_id, ai.vision_model, locale)
    except AiUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"ai_unavailable:{exc.reason}") from exc

    auto_apply = user_settings(user.settings).pantry_photo_auto_apply if apply is None else apply
    applied: list[dict] = []
    if auto_apply:
        applied = [_pantry_payload(i) for i in _merge_scanned(db, household_id, scanned)]
        db.commit()
    return {"found": [s.model_dump() for s in scanned], "applied": applied}


class SuggestIn(BaseModel):
    ingredients: list[str] = Field(default_factory=list, max_length=30)
    meal_type: str | None = None
    use_pantry: bool = False


@router.post("/pantry/suggest")
def suggest_dishes(payload: SuggestIn, user: CurrentUser, db: Db) -> list[dict]:
    """"What can I make with beef mince, potatoes and onions?", without using AI."""
    household = household_of(db, user)
    ingredients = list(payload.ingredients)
    if payload.use_pantry:
        ingredients += load_stock(db, household.id).names()
    return recipes_matching_ingredients(db, household, ingredients, payload.meal_type)


# Running low


@router.get("/pantry/restock")
def get_restock(user: CurrentUser, db: Db) -> list[dict]:
    require_feature(user, "restock")
    return restock_suggestions(db, household_of(db, user), user)


class RestockAddIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


@router.post("/pantry/restock/add", status_code=201)
def restock_add(payload: RestockAddIn, user: CurrentUser, db: Db) -> dict:
    """Puts it on the current list. People may buy what they want, but they will hear about it."""
    item = add_manual_item(db, user.household_id, payload.name)
    warning = conflicting_allergy(payload.name, household_profile(db, household_of(db, user)).allergies)
    db.commit()
    return {"id": item.id, "list_id": item.list_id, "allergy_warning": warning}


class RestockSnoozeIn(BaseModel):
    key: str = Field(min_length=1, max_length=200)


@router.post("/pantry/restock/snooze")
def restock_snooze(payload: RestockSnoozeIn, user: CurrentUser, db: Db) -> dict:
    """"Not now". It comes back in a week if it is still relevant."""
    snooze(db, household_of(db, user), payload.key)
    db.commit()
    return {"ok": True}


@router.get("/waste")
def waste(user: CurrentUser, db: Db) -> dict:
    require_feature(user, "waste_agent")
    return waste_suggestions(db, household_of(db, user))


# Diet coach


def _coach_enabled(db, user) -> bool:
    own = user_settings(user.settings).diet_coach_enabled
    return household_settings(household_of(db, user).settings).diet_coach_enabled if own is None else own


@router.get("/diet/report")
def get_diet_report(user: CurrentUser, db: Db, days: int = 28) -> dict:
    require_feature(user, "diet_coach")
    report = diet_report(db, household_of(db, user), max(7, min(days, 365)))
    report["coach_enabled"] = _coach_enabled(db, user)
    return report


@router.post("/diet/advice")
def diet_advice(user: CurrentUser, db: Db) -> dict:
    require_feature(user, "diet_coach")
    if not _coach_enabled(db, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "diet_coach_off")
    from app.ai.features import write_diet_advice

    report = diet_report(db, household_of(db, user))
    try:
        return {"advice": write_diet_advice(report, user.household_id, user.id, user.locale), "report": report}
    except AiUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"ai_unavailable:{exc.reason}") from exc


# Budget


class ExpenseIn(BaseModel):
    day: date
    amount: float = Field(gt=0)
    savings: float = Field(default=0, ge=0)
    note: str | None = Field(default=None, max_length=200)


@router.get("/budget")
def get_budget(user: CurrentUser, db: Db) -> dict:
    return dashboard(db, household_of(db, user), user)


@router.get("/expenses")
def list_expenses(user: CurrentUser, db: Db) -> list[dict]:
    rows = db.scalars(select(Expense).where(Expense.household_id == user.household_id).order_by(Expense.day.desc()).limit(100))
    return [{"id": e.id, "day": e.day, "amount": e.amount, "savings": e.savings, "note": e.note, "list_id": e.list_id} for e in rows]


@router.post("/expenses", status_code=201)
def add_expense(payload: ExpenseIn, user: CurrentUser, db: Db) -> dict:
    """For purchases outside a list, and for entering last year's spending to compare against."""
    expense = Expense(household_id=user.household_id, user_id=user.id, **payload.model_dump())
    db.add(expense)
    db.commit()
    return {"id": expense.id}


@router.delete("/expenses/{expense_id}")
def delete_expense(expense_id: int, user: CurrentUser, db: Db) -> dict:
    expense = db.get(Expense, expense_id)
    if expense is None or expense.household_id != user.household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    db.delete(expense)
    db.commit()
    return {"ok": True}


# Assistant


class AskIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    mode: str = Field(default="auto", pattern="^(auto|pantry|mealie|direct|diet|waste)$")


@router.post("/assistant")
def ask_assistant(payload: AskIn, user: CurrentUser, db: Db) -> dict:
    require_feature(user, "assistant")
    from app.ai.assistant import ask

    return ask(db, user, payload.message, payload.mode)


@router.get("/assistant/history")
def assistant_history(user: CurrentUser, db: Db) -> list[dict]:
    rows = db.scalars(select(ChatMessage).where(ChatMessage.user_id == user.id).order_by(ChatMessage.id.desc()).limit(50))
    return [
        {"id": m.id, "role": m.role, "content": m.content, "engine": m.engine, "proposals": m.proposals, "created_at": m.created_at}
        for m in reversed(list(rows))
    ]


class ProposalAnswerIn(BaseModel):
    approve: bool


@router.post("/assistant/messages/{message_id}/proposals/{index}")
def answer_proposal(message_id: int, index: int, payload: ProposalAnswerIn, user: CurrentUser, db: Db) -> dict:
    """A person says yes or no to something the assistant offered to do."""
    require_feature(user, "assistant")
    try:
        result = proposals.answer(db, user, message_id, index, payload.approve)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found") from exc
    except proposals.ProposalError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    return result


@router.delete("/assistant/history")
def clear_history(user: CurrentUser, db: Db) -> dict:
    for row in db.scalars(select(ChatMessage).where(ChatMessage.user_id == user.id)):
        db.delete(row)
    db.commit()
    return {"ok": True}
