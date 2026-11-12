"""Runs the enabled sources over a list of search terms and stores what they return."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.db import local_today, utcnow
from app.crawlers.policy import CrawlDenied, PoliteClient
from app.crawlers.sources import SOURCES, OfferSource, ParsedOffer
from app.models import CrawlRun, CrawlSource, Offer, PantryItem, PriceAlert, PricePoint, Store, TrackedItem
from app.services.catalog import CRAWL_TERMS
from app.services.stores import store_for_chain

log = logging.getLogger(__name__)

# Shelf prices have no end date. An old one must not pass for a current price forever.
SHELF_PRICE_MAX_AGE_DAYS = 14
OFFER_RETENTION_DAYS = 60


def build_terms(db: Session, max_terms: int, today: date | None = None) -> list[str]:
    """What people asked to watch comes first, then what the planner cooks from.

    When the limit is too low for the whole catalog, the rest takes turns from day to day, so
    that nothing at the end of the list goes uncrawled for good.
    """
    terms: dict[str, None] = {}
    for query in db.scalars(select(PriceAlert.query).where(PriceAlert.active.is_(True))):
        terms.setdefault(query.strip().lower())
    for query in db.scalars(select(TrackedItem.query)):
        terms.setdefault(query.strip().lower())
    for name in db.scalars(select(PantryItem.name).where(PantryItem.min_quantity.is_not(None))):
        terms.setdefault(name.strip().lower())
    watched = len(terms)
    room = max(0, max_terms - watched)
    if room:
        day = (today or local_today()).toordinal()
        start = day * room % len(CRAWL_TERMS)
        rotated = CRAWL_TERMS[start:] + CRAWL_TERMS[:start]
        for term in rotated:
            if len(terms) >= max_terms:
                break
            terms.setdefault(term)
    return list(terms)[:max_terms]


def upsert_offer(db: Session, source: str, parsed: ParsedOffer, stores: dict[str, Store]) -> Offer:
    store = store_for_chain(db, parsed.chain, stores)
    offer = db.scalar(
        select(Offer).where(Offer.source == source, Offer.external_id == parsed.external_id)
    )
    now = utcnow()
    if offer is None:
        offer = Offer(source=source, external_id=parsed.external_id, first_seen_at=now)
        db.add(offer)
    offer.store_id = store.id
    offer.title = parsed.title[:300]
    offer.description = parsed.description
    offer.search_text = parsed.search_text
    offer.kind = parsed.kind
    offer.price = parsed.price
    offer.pre_price = parsed.pre_price
    offer.pack_quantity = parsed.pack_quantity
    offer.pack_unit = parsed.pack_unit
    offer.unit_price = parsed.unit_price
    offer.unit_basis = parsed.unit_basis
    offer.membership_required = parsed.membership_required
    offer.valid_from = parsed.valid_from
    offer.valid_to = parsed.valid_to
    offer.url = parsed.url
    offer.last_seen_at = now

    today = local_today()
    point_exists = db.scalar(
        select(PricePoint.id).where(
            PricePoint.source == source,
            PricePoint.external_id == parsed.external_id,
            PricePoint.observed_on == today,
        )
    )
    if point_exists is None:
        db.add(
            PricePoint(
                source=source,
                external_id=parsed.external_id,
                store_id=store.id,
                search_text=parsed.search_text,
                kind=parsed.kind,
                price=parsed.price,
                unit_price=parsed.unit_price,
                unit_basis=parsed.unit_basis,
                observed_on=today,
            )
        )
    return offer


def crawl_source(
    db: Session,
    source: OfferSource,
    terms: list[str],
    delay_seconds: float,
    transport: httpx.BaseTransport | None = None,
    sleep=None,
) -> CrawlRun:
    run = CrawlRun(source=source.info.slug, terms=len(terms))
    db.add(run)
    db.commit()

    kwargs = {"sleep": sleep} if sleep else {}
    stores: dict[str, Store] = {}
    seen: set[str] = set()
    errors: list[str] = []
    with PoliteClient(source.info.rules, delay_seconds, transport=transport, **kwargs) as client:
        for term in terms:
            try:
                for parsed in source.search(client, term):
                    if parsed.external_id in seen:
                        continue
                    seen.add(parsed.external_id)
                    upsert_offer(db, source.info.slug, parsed, stores)
                db.commit()
            except CrawlDenied as exc:
                # A policy denial is an answer, not a failure. Log it and stop bothering the site.
                log.warning("crawl of %s stopped by policy: %s", source.info.slug, exc)
                errors.append(f"{exc.reason}: {exc.url}")
                db.rollback()
                break
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                # One bad page should not lose the rest of the run. A site redesign shows up
                # here as many of these, and the admin page shows the run as "partial".
                log.warning("crawl of %s failed for %r: %s", source.info.slug, term, exc)
                errors.append(f"{term}: {exc}")
                db.rollback()
        run.requests = client.requests_made
        run.denied_by_policy = client.denied

    run.offers_seen = len(seen)
    run.finished_at = utcnow()
    if run.denied_by_policy:
        run.status = "denied"
    elif errors and not seen:
        run.status = "failed"
    elif errors:
        run.status = "partial"
    else:
        run.status = "ok"
    run.error = "\n".join(errors[:10]) or None

    state = db.get(CrawlSource, source.info.slug)
    if state:
        state.last_run_at = run.finished_at
        state.last_status = run.status
    db.commit()
    return run


def prune_old_offers(db: Session, today: date | None = None) -> None:
    """Price points are kept as history; the offers themselves do not need to linger."""
    cutoff = (today or local_today()) - timedelta(days=OFFER_RETENTION_DAYS)
    db.execute(delete(Offer).where(Offer.valid_to.is_not(None), Offer.valid_to < cutoff))
    db.commit()


def crawl_all(db: Session, delay_seconds: float, max_terms: int) -> list[CrawlRun]:
    terms = build_terms(db, max_terms)
    enabled = {state.slug for state in db.scalars(select(CrawlSource).where(CrawlSource.enabled.is_(True)))}
    runs = [
        crawl_source(db, source, terms, delay_seconds)
        for slug, source in SOURCES.items()
        if slug in enabled
    ]
    prune_old_offers(db)
    return runs
