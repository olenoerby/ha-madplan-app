"""First-run setup. Safe to run on every start: it only adds what is missing."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import ha
from app.crawlers.sources import SOURCES
from app.models import CrawlSource, Household, Store
from app.services.recipes import seed_builtin_recipes
from app.services.stores import KNOWN_STORES

log = logging.getLogger(__name__)


def seed_stores(db: Session) -> None:
    existing = {s.slug: s for s in db.scalars(select(Store))}
    for slug, name, enabled in KNOWN_STORES:
        store = existing.get(slug)
        if store is None:
            db.add(Store(slug=slug, name=name, default_enabled=enabled, is_known_grocery=True))
        elif not store.is_known_grocery:
            # A crawler got there first and added it as an unknown chain.
            store.name, store.default_enabled, store.is_known_grocery = name, enabled, True


def seed_sources(db: Session) -> None:
    for slug in SOURCES:
        if db.get(CrawlSource, slug) is None:
            db.add(CrawlSource(slug=slug, enabled=True))


def ensure_household(db: Session) -> None:
    """The one household. People are added when they first open the app from Home Assistant."""
    if db.scalar(select(func.count(Household.id))) == 0:
        # Named as the home is in Home Assistant, when it can be asked.
        db.add(Household(name=ha.ha_config().get("location_name") or "Home", settings={}))


def bootstrap(db: Session) -> None:
    seed_stores(db)
    seed_sources(db)
    seed_builtin_recipes(db)
    ensure_household(db)
    db.commit()
