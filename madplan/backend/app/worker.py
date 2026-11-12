"""Background jobs: crawling, alerts, buy signals, the pantry, Mealie sync and automatic plans.

Runs as its own container. It reads the schedule from the database on every tick, so a change
on the admin page takes effect within half a minute, without a restart.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.ai.engine import AiUnavailable
from app.core.db import local_hour, local_today, session_scope, utcnow
from app.crawlers.runner import crawl_all
from app.integrations.mealie import MealieError
from app.models import AppSetting, ChatMessage, Household, MealPlan, Notification, PriceAlert, Recipe, User, UserSession
from app.services.alerts import apply_web_result, evaluate_all_alerts, run_buy_signals
from app.services.app_settings import get_ai_settings, get_schedule, mealie_client, mealie_config
from app.services.household_care import run_waste_check
from app.services.mealie_pull import pull_from_mealie
from app.services.mealie_sync import process_pending, pull_recipes, remember_group, remind_unsent
from app.services.new_recipes import find_new_recipes
from app.services.pantry import job_pantry
from app.services.planner import (
    NoRecipes,
    PlanRequest,
    fill_empty_slots,
    generate_plan,
    missing_slots,
    notify_plan_ready,
    recipes_are_short,
    refresh_open_plans,
    resolve_dates,
)
from app.services.restock import run_restock_check
from app.settings_schema import household_settings, user_settings

log = logging.getLogger("madplan.worker")

TICK_SECONDS = 30
_STATE_KEY = "worker_state"
_REQUESTS_KEY = "worker_requests"


def _state(db: Session) -> dict:
    row = db.get(AppSetting, _STATE_KEY)
    return dict(row.value) if row else {}


def _mark(db: Session, job: str, when: datetime) -> None:
    row = db.get(AppSetting, _STATE_KEY)
    if row is None:
        db.add(AppSetting(key=_STATE_KEY, value={job: when.isoformat()}))
    else:
        row.value = {**(row.value or {}), job: when.isoformat()}


def is_due(state: dict, job: str, interval: timedelta, now: datetime) -> bool:
    last = state.get(job)
    return last is None or now - datetime.fromisoformat(last) >= interval


def _crawl_requested(db: Session) -> bool:
    """Reads and clears the admin's "crawl now" request."""
    row = db.get(AppSetting, _REQUESTS_KEY)
    if not row or not (row.value or {}).get("crawl"):
        return False
    row.value = {**row.value, "crawl": False}
    return True


def job_crawl(db: Session) -> None:
    schedule = get_schedule(db)
    runs = crawl_all(db, schedule.crawl_request_delay_seconds, schedule.crawl_max_terms)
    log.info("crawl finished: %s", [(r.source, r.status, r.offers_seen) for r in runs])
    evaluate_all_alerts(db)


def job_mealie_pull(db: Session) -> None:
    for household in db.scalars(select(Household)):
        config = mealie_config(db, household.id)
        client = mealie_client(db, household.id)
        if not config or not client or not config.sync_enabled:
            continue
        try:
            with client:
                added = pull_recipes(db, household.id, client)
                remember_group(db, household.id, client)
                # Mealie has the last word on its planner and its shopping list.
                changes = pull_from_mealie(db, household.id, client, config.shopping_list_id)
            db.commit()
            if added:
                log.info("imported %d recipes from Mealie for household %d", added, household.id)
            if any(changes.values()):
                log.info("took changes from Mealie for household %d: %s", household.id, changes)
        except MealieError as exc:
            db.rollback()
            log.warning("could not read from Mealie: %s", exc)


def job_price_hunt(db: Session) -> None:
    """Web prices for alerts that asked for it. Stops by itself when the AI budget runs out."""
    from app.ai.features import hunt_web_prices

    rows = db.execute(
        select(PriceAlert, User)
        .join(User, PriceAlert.user_id == User.id)
        .where(PriceAlert.active.is_(True), PriceAlert.use_web_search.is_(True), User.is_active.is_(True))
    ).all()
    for alert, user in rows:
        if not user_settings(user.settings).features.price_alerts:
            continue
        try:
            results = hunt_web_prices(alert.query, alert.max_price, user.household_id, user.id)
        except AiUnavailable as exc:
            log.info("price hunt stopped: %s", exc.reason)
            return
        alert.last_web_check_at = utcnow()
        match = next((r for r in results if r.price <= alert.max_price), None)
        if match:
            apply_web_result(db, alert, user, match.model_dump())
        db.commit()


def _more_recipes(db: Session, household: Household, meal_types: list[str], day, days: int) -> list[Recipe]:
    """More real recipes from the web when there are too few to choose from, and the budget allows."""
    count = get_ai_settings(db).plan_ai_candidates
    if not count or not recipes_are_short(db, household, meal_types, days):
        return []
    try:
        return find_new_recipes(db, household, None, meal_types, count, day).saved
    except AiUnavailable as exc:
        log.info("automatic plan without a recipe search: %s", exc.reason)
        return []


def job_recipe_search(db: Session) -> None:
    """Looks for recipes around the current offers, for the "New recipes" page and for plans."""
    count = get_ai_settings(db).plan_ai_candidates
    if not count:
        return
    for household in db.scalars(select(Household)):
        members = db.scalars(select(User).where(User.household_id == household.id, User.is_active.is_(True))).all()
        interested = [m for m in members if user_settings(m.settings).features.new_recipes]
        if not interested:
            continue
        meal_types = list(household_settings(household.settings).default_meal_types)
        try:
            result = find_new_recipes(db, household, None, meal_types, count, local_today(), interested[0].locale)
        except AiUnavailable as exc:
            log.info("recipe search stopped: %s", exc.reason)
            return
        db.commit()
        log.info("found %d recipes for household %d", len(result.saved), household.id)


def job_auto_plan(db: Session) -> None:
    """Makes next week's plan for households that have none yet, and asks people to vote."""
    for household in db.scalars(select(Household)):
        hs = household_settings(household.settings)
        dates = resolve_dates("full_week")
        exists = db.scalar(
            select(MealPlan).where(MealPlan.household_id == household.id, MealPlan.start_date == min(dates)).order_by(MealPlan.id.desc()).limit(1)
        )
        if exists is not None and missing_slots(exists) > 0:
            # Someone put a dish on next week by hand, which made the plan. The rest is filled in
            # around it, and voted on as usual.
            if fill_empty_slots(db, exists, None):
                notify_plan_ready(db, exists)
                db.commit()
                log.info("filled next week's plan for household %d", household.id)
            continue
        if exists is None and db.scalar(select(User.id).where(User.household_id == household.id).limit(1)):
            meal_types = list(hs.default_meal_types)
            extra = _more_recipes(db, household, meal_types, min(dates), len(dates))
            try:
                plan = generate_plan(db, household, PlanRequest(dates=dates, meal_types=meal_types), None, extra)
            except NoRecipes:
                log.info("no recipes to plan from for household %d", household.id)
                continue
            notify_plan_ready(db, plan)
            db.commit()
            log.info("created next week's plan for household %d", household.id)


def job_cleanup(db: Session) -> None:
    now = utcnow()
    db.execute(delete(UserSession).where(UserSession.expires_at < now))
    # What people said and were told is theirs, and it is not kept for longer than it is useful.
    schedule = get_schedule(db)
    if schedule.chat_retention_days:
        db.execute(delete(ChatMessage).where(ChatMessage.created_at < now - timedelta(days=schedule.chat_retention_days)))
    if schedule.notification_retention_days:
        db.execute(delete(Notification).where(Notification.created_at < now - timedelta(days=schedule.notification_retention_days)))


def run_due_jobs(now: datetime | None = None) -> list[str]:
    """One pass over the schedule. Each job gets its own transaction, so one failing is contained."""
    now = now or utcnow()
    with session_scope() as db:
        schedule = get_schedule(db)
        ai = get_ai_settings(db)
        state = _state(db)
        crawl_requested = _crawl_requested(db)

    today = local_today()
    auto_plan_due = (
        schedule.auto_plan_enabled
        and today.weekday() == schedule.auto_plan_weekday
        and local_hour(now) >= schedule.auto_plan_hour
    )
    jobs: list[tuple[str, bool, Callable[[Session], None]]] = [
        ("crawl", crawl_requested or (schedule.crawl_enabled and is_due(state, "crawl", timedelta(hours=schedule.crawl_interval_hours), now)), job_crawl),
        ("alerts", is_due(state, "alerts", timedelta(hours=schedule.alert_interval_hours), now), evaluate_all_alerts),
        (
            "buy_signals",
            schedule.buy_signals_enabled and is_due(state, "buy_signals", timedelta(hours=schedule.alert_interval_hours), now),
            run_buy_signals,
        ),
        ("reprice", schedule.reprice_after_crawl and state.get("crawl", "") > state.get("reprice", ""), refresh_open_plans),
        ("pantry", is_due(state, "pantry", timedelta(hours=6), now), job_pantry),
        ("restock", is_due(state, "restock", timedelta(hours=schedule.restock_check_hours), now), run_restock_check),
        ("waste", is_due(state, "waste", timedelta(hours=schedule.waste_check_hours), now), run_waste_check),
        ("mealie_push", True, process_pending),
        ("mealie_reminder", is_due(state, "mealie_reminder", timedelta(hours=3), now), remind_unsent),
        ("mealie_pull", is_due(state, "mealie_pull", timedelta(minutes=schedule.mealie_sync_minutes), now), job_mealie_pull),
        (
            "price_hunt",
            ai.enabled and ai.features.price_hunt and is_due(state, "price_hunt", timedelta(hours=schedule.price_hunt_interval_hours), now),
            job_price_hunt,
        ),
        (
            "recipe_search",
            schedule.recipe_search_enabled
            and ai.enabled
            and ai.features.recipe_finder
            and bool(state.get("crawl"))
            and is_due(state, "recipe_search", timedelta(hours=schedule.recipe_search_interval_hours), now),
            job_recipe_search,
        ),
        ("auto_plan", auto_plan_due and is_due(state, "auto_plan", timedelta(hours=23), now), job_auto_plan),
        ("cleanup", is_due(state, "cleanup", timedelta(days=1), now), job_cleanup),
    ]
    ran: list[str] = []
    for name, due, job in jobs:
        if not due:
            continue
        try:
            with session_scope() as db:
                job(db)
                _mark(db, name, now)
            ran.append(name)
        except Exception:
            log.exception("job %s failed", name)
    return ran


def run_in_background(stop: threading.Event) -> threading.Thread:
    """The jobs, in a thread of the web server's process. One process is all the app runs."""

    def loop() -> None:
        log.info("background jobs started")
        while not stop.is_set():
            try:
                run_due_jobs()
            except Exception:
                log.exception("background jobs failed, and will try again")
            stop.wait(TICK_SECONDS)
        log.info("background jobs stopped")

    thread = threading.Thread(target=loop, name="madplan-jobs", daemon=True)
    thread.start()
    return thread


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("worker started")
    while not stop.is_set():
        run_due_jobs()
        stop.wait(TICK_SECONDS)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
