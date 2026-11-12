"""Store chains, which ones a household or user uses, and how far away they are."""

from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Household, HouseholdStore, Store, User, UserStore
from app.services.text import normalize

# slug, display name, enabled by default
KNOWN_STORES: tuple[tuple[str, str, bool], ...] = (
    ("netto", "Netto", True),
    ("rema1000", "Rema 1000", True),
    ("coop365", "Coop 365", True),
    ("lovbjerg", "Løvbjerg", True),
    ("meny", "Meny", True),
    ("lidl", "Lidl", True),
    ("bilka", "Bilka", False),
    ("foetex", "Føtex", False),
    ("spar", "Spar", False),
    ("superbrugsen", "SuperBrugsen", False),
    ("kvickly", "Kvickly", False),
)

# The spellings the sources actually use. "365discount" is what Coop 365 is called on both.
_CHAIN_ALIASES: dict[str, str] = {
    "netto": "netto",
    "rema 1000": "rema1000",
    "rema1000": "rema1000",
    "coop 365": "coop365",
    "coop365": "coop365",
    "365discount": "coop365",
    "365 discount": "coop365",
    "løvbjerg": "lovbjerg",
    "lovbjerg": "lovbjerg",
    "meny": "meny",
    "lidl": "lidl",
    "bilka": "bilka",
    "føtex": "foetex",
    "foetex": "foetex",
    "spar": "spar",
    "superbrugsen": "superbrugsen",
    "kvickly": "kvickly",
}


def chain_slug(chain: str) -> str:
    key = normalize(chain)
    if key in _CHAIN_ALIASES:
        return _CHAIN_ALIASES[key]
    return key.replace(" ", "-")[:80] or "unknown"


def store_for_chain(db: Session, chain: str, cache: dict[str, Store]) -> Store:
    """Finds the store for a chain name, adding unknown chains as non-grocery."""
    slug = chain_slug(chain)
    if slug in cache:
        return cache[slug]
    store = db.scalar(select(Store).where(Store.slug == slug))
    if store is None:
        store = Store(slug=slug, name=chain.strip()[:120], default_enabled=False, is_known_grocery=False)
        db.add(store)
        db.flush()
    cache[slug] = store
    return store


@dataclass
class StoreChoice:
    store: Store
    enabled: bool
    favorite: bool
    distance_km: float | None
    branch_name: str | None
    lat: float | None
    lon: float | None


def household_store_choices(
    db: Session,
    household: Household,
    user: User | None = None,
    position: tuple[float, float] | None = None,
) -> list[StoreChoice]:
    """Every known store with the effective choice for this household or user.

    ``position`` replaces the distance from home with the distance from the phone, when a
    user has allowed that and the browser sent one.
    """
    links = {
        link.store_id: link
        for link in db.scalars(select(HouseholdStore).where(HouseholdStore.household_id == household.id))
    }
    overrides = {}
    if user is not None:
        overrides = {o.store_id: o for o in db.scalars(select(UserStore).where(UserStore.user_id == user.id))}

    choices: list[StoreChoice] = []
    for store in db.scalars(select(Store).order_by(Store.name)):
        link = links.get(store.id)
        override = overrides.get(store.id)
        if not store.is_known_grocery and link is None and override is None:
            continue
        enabled = link.enabled if link else store.default_enabled
        favorite = link.favorite if link else False
        if override is not None:
            enabled = enabled if override.enabled is None else override.enabled
            favorite = favorite if override.favorite is None else override.favorite

        distance = link.distance_km if link else None
        if position and link and link.lat is not None and link.lon is not None:
            distance = road_distance_km(position[0], position[1], link.lat, link.lon)
        choices.append(
            StoreChoice(
                store=store,
                enabled=enabled,
                favorite=favorite,
                distance_km=distance,
                branch_name=link.branch_name if link else None,
                lat=link.lat if link else None,
                lon=link.lon if link else None,
            )
        )
    return sorted(
        choices,
        key=lambda c: (not c.favorite, not c.enabled, c.distance_km if c.distance_km is not None else 1e9, c.store.name),
    )


def enabled_store_ids(db: Session, household: Household, user: User | None = None) -> set[int]:
    return {c.store.id for c in household_store_choices(db, household, user) if c.enabled}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def road_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance with the usual allowance for roads not being straight."""
    return round(haversine_km(lat1, lon1, lat2, lon2) * 1.3, 2)
