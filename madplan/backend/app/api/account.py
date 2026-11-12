"""A user's own settings, tokens and notifications, and the household they belong to."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, Db, ManagerUser, household_of
from app.core import ha
from app.core.db import utcnow
from app.core.security import new_token, token_hash
from app.models import ApiToken, HouseholdStore, Notification, Store, TrackedItem, User, UserStore
from app.services import geo
from app.services.favorites import learn_taste
from app.services.privacy import delete_user, export_user_data
from app.services.stores import household_store_choices, road_distance_km
from app.settings_schema import HouseholdSettings, UserSettings, household_settings, user_settings

router = APIRouter(tags=["account"])


class ProfileIn(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)


@router.patch("/me")
def update_profile(payload: ProfileIn, user: CurrentUser, db: Db) -> dict:
    if payload.display_name is not None:
        user.display_name = payload.display_name
    db.commit()
    return {"display_name": user.display_name, "locale": user.locale}


@router.get("/me/settings")
def get_my_settings(user: CurrentUser) -> dict:
    return user_settings(user.settings).model_dump(mode="json")


@router.put("/me/settings")
def put_my_settings(payload: UserSettings, user: CurrentUser, db: Db) -> dict:
    user.settings = payload.model_dump(mode="json")
    db.commit()
    return user.settings


@router.get("/me/notify-services")
def my_notify_services(user: CurrentUser) -> dict:
    """Where messages can go in Home Assistant, and where they go when a person has not chosen."""
    return {"available": ha.notify_services(), "default": ha.options().notify_service}


@router.get("/me/export")
def export_me(user: CurrentUser, db: Db) -> Response:
    """Everything kept about this person and their household, as a file to take along."""
    import json

    body = json.dumps(jsonable_encoder(export_user_data(db, user)), ensure_ascii=False, indent=2)
    return Response(
        body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="madplan-{user.username}.json"'},
    )


class DeleteMeIn(BaseModel):
    # Typed out, so that nobody leaves by accident.
    confirm_username: str


@router.post("/me/delete")
def delete_me(payload: DeleteMeIn, response: Response, user: CurrentUser, db: Db) -> dict:
    if payload.confirm_username.strip().lower() != user.username:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "confirmation_mismatch")
    # The person stays in Home Assistant. Opening the app again starts them afresh.
    delete_user(db, user)
    db.commit()
    return {"ok": True}


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.get("/me/tokens")
def list_tokens(user: CurrentUser, db: Db) -> list[dict]:
    rows = db.scalars(select(ApiToken).where(ApiToken.user_id == user.id))
    return [{"id": t.id, "name": t.name, "created_at": t.created_at, "last_used_at": t.last_used_at} for t in rows]


@router.post("/me/tokens", status_code=201)
def create_token(payload: TokenIn, user: CurrentUser, db: Db) -> dict:
    """The token is only ever shown in this response. MadPlan stores just its hash."""
    token = new_token()
    row = ApiToken(user_id=user.id, name=payload.name, token_hash=token_hash(token))
    db.add(row)
    db.commit()
    return {"id": row.id, "name": row.name, "token": token}


@router.delete("/me/tokens/{token_id}")
def delete_token(token_id: int, user: CurrentUser, db: Db) -> dict:
    row = db.get(ApiToken, token_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    db.delete(row)
    db.commit()
    return {"ok": True}


class TrackedItemIn(BaseModel):
    query: str = Field(min_length=1, max_length=160)
    priority: int = Field(default=0, ge=-3, le=3)


@router.get("/me/items")
def list_items(user: CurrentUser, db: Db) -> list[dict]:
    rows = db.scalars(select(TrackedItem).where(TrackedItem.user_id == user.id).order_by(TrackedItem.query))
    return [{"id": i.id, "query": i.query, "priority": i.priority} for i in rows]


@router.put("/me/items")
def upsert_item(payload: TrackedItemIn, user: CurrentUser, db: Db) -> dict:
    query = payload.query.strip().lower()
    row = db.scalar(select(TrackedItem).where(TrackedItem.user_id == user.id, TrackedItem.query == query))
    if row is None:
        row = TrackedItem(user_id=user.id, query=query)
        db.add(row)
    row.priority = payload.priority
    db.commit()
    return {"id": row.id, "query": row.query, "priority": row.priority}


@router.delete("/me/items/{item_id}")
def delete_item(item_id: int, user: CurrentUser, db: Db) -> dict:
    row = db.get(TrackedItem, item_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/notifications")
def list_notifications(user: CurrentUser, db: Db, unread_only: bool = False) -> list[dict]:
    statement = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        statement = statement.where(Notification.read_at.is_(None))
    rows = db.scalars(statement.order_by(Notification.id.desc()).limit(100))
    return [{"id": n.id, "kind": n.kind, "data": n.data, "created_at": n.created_at, "read": n.read_at is not None} for n in rows]


@router.post("/notifications/read-all")
def read_all(user: CurrentUser, db: Db) -> dict:
    for row in db.scalars(select(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))):
        row.read_at = utcnow()
    db.commit()
    return {"ok": True}


@router.post("/notifications/{notification_id}/read")
def read_one(notification_id: int, user: CurrentUser, db: Db) -> dict:
    row = db.get(Notification, notification_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    row.read_at = utcnow()
    db.commit()
    return {"ok": True}


# Household


@router.get("/household")
def get_household(user: CurrentUser, db: Db) -> dict:
    household = household_of(db, user)
    members = db.scalars(select(User).where(User.household_id == household.id, User.is_active.is_(True)))
    return {
        "id": household.id,
        "name": household.name,
        "settings": household_settings(household.settings).model_dump(mode="json"),
        "members": [{"id": m.id, "display_name": m.display_name, "role": m.role} for m in members],
        "can_edit": user.manages_household,
    }


class HouseholdIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    settings: HouseholdSettings | None = None


@router.put("/household")
def put_household(payload: HouseholdIn, user: ManagerUser, db: Db) -> dict:
    household = household_of(db, user)
    if payload.name is not None:
        household.name = payload.name
    if payload.settings is not None:
        household.settings = payload.settings.model_dump(mode="json")
        _refresh_distances(db, household)
    db.commit()
    return {"id": household.id, "name": household.name, "settings": household.settings}


def _refresh_distances(db, household) -> None:
    hs = household_settings(household.settings)
    if hs.lat is None or hs.lon is None:
        return
    for link in db.scalars(select(HouseholdStore).where(HouseholdStore.household_id == household.id)):
        if link.lat is not None and link.lon is not None:
            link.distance_km = road_distance_km(hs.lat, hs.lon, link.lat, link.lon)


@router.get("/household/taste")
def household_taste(user: CurrentUser, db: Db) -> dict:
    taste = learn_taste(db, user.household_id)
    return {"liked": taste.liked, "rejected": taste.rejected, "members": taste.members}


def _store_row(choice) -> dict:
    return {
        "id": choice.store.id,
        "slug": choice.store.slug,
        "name": choice.store.name,
        "enabled": choice.enabled,
        "favorite": choice.favorite,
        "distance_km": choice.distance_km,
        "branch_name": choice.branch_name,
        "lat": choice.lat,
        "lon": choice.lon,
        "known_grocery": choice.store.is_known_grocery,
    }


@router.get("/stores")
def list_stores(user: CurrentUser, db: Db, lat: float | None = None, lon: float | None = None) -> list[dict]:
    """Effective stores for this user. lat and lon come from the phone when the user allows it."""
    position = (lat, lon) if lat is not None and lon is not None and user_settings(user.settings).use_phone_location else None
    return [_store_row(c) for c in household_store_choices(db, household_of(db, user), user, position)]


class HouseholdStoreIn(BaseModel):
    enabled: bool | None = None
    favorite: bool | None = None
    distance_km: float | None = Field(default=None, ge=0)
    branch_name: str | None = Field(default=None, max_length=160)
    lat: float | None = None
    lon: float | None = None


@router.put("/household/stores/{store_id}")
def put_household_store(store_id: int, payload: HouseholdStoreIn, user: ManagerUser, db: Db) -> dict:
    store = db.get(Store, store_id)
    if store is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    link = db.get(HouseholdStore, (user.household_id, store_id))
    if link is None:
        link = HouseholdStore(household_id=user.household_id, store_id=store_id, enabled=store.default_enabled, favorite=False)
        db.add(link)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(link, field, value)
    household = household_of(db, user)
    hs = household_settings(household.settings)
    if link.lat is not None and link.lon is not None and hs.lat is not None and hs.lon is not None and payload.distance_km is None:
        link.distance_km = road_distance_km(hs.lat, hs.lon, link.lat, link.lon)
    db.commit()
    return {"ok": True}


class UserStoreIn(BaseModel):
    enabled: bool | None = None
    favorite: bool | None = None


@router.put("/me/stores/{store_id}")
def put_my_store(store_id: int, payload: UserStoreIn, user: CurrentUser, db: Db) -> dict:
    """A personal override. Sending null for a field goes back to the household's choice."""
    if db.get(Store, store_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    row = db.get(UserStore, (user.id, store_id))
    if row is None:
        row = UserStore(user_id=user.id, store_id=store_id)
        db.add(row)
    row.enabled, row.favorite = payload.enabled, payload.favorite
    db.commit()
    return {"ok": True}


class AddressIn(BaseModel):
    address: str = Field(min_length=3, max_length=300)


@router.post("/household/geocode")
def geocode_address(payload: AddressIn, user: ManagerUser, db: Db) -> dict:
    household = household_of(db, user)
    hs = household_settings(household.settings)
    if not hs.allow_osm_lookup:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "osm_lookup_disabled")
    try:
        found = geo.geocode(payload.address)
    except geo.GeoError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "geocoding_failed") from exc
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "address_not_found")
    hs.address, hs.lat, hs.lon = payload.address, found[0], found[1]
    household.settings = hs.model_dump(mode="json")
    _refresh_distances(db, household)
    db.commit()
    return {"lat": found[0], "lon": found[1]}


@router.post("/household/stores/nearby")
def find_nearby_stores(user: ManagerUser, db: Db, radius_km: float = 15) -> dict:
    """Finds the nearest branch of each chain from OpenStreetMap, and saves the distances."""
    household = household_of(db, user)
    hs = household_settings(household.settings)
    if not hs.allow_osm_lookup:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "osm_lookup_disabled")
    if hs.lat is None or hs.lon is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "home_position_missing")
    stores = list(db.scalars(select(Store).where(Store.is_known_grocery.is_(True))))
    try:
        branches = geo.nearest_branches(hs.lat, hs.lon, radius_km, stores)
    except geo.GeoError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "lookup_failed") from exc
    updated = 0
    for store in stores:
        branch = branches.get(store.slug)
        if branch is None:
            continue
        link = db.get(HouseholdStore, (household.id, store.id))
        if link is None:
            link = HouseholdStore(household_id=household.id, store_id=store.id, enabled=store.default_enabled, favorite=False)
            db.add(link)
        link.branch_name, link.lat, link.lon = branch["name"], branch["lat"], branch["lon"]
        link.distance_km = road_distance_km(hs.lat, hs.lon, branch["lat"], branch["lon"])
        updated += 1
    db.commit()
    return {"updated": updated}
