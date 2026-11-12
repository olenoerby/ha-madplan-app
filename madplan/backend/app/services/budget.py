"""Budget dashboard and household economy. Comparisons appear only once real data backs them."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import local_today
from app.models import Expense, Household, MealPlan, ShoppingList
from app.settings_schema import household_settings, user_settings


def _spent(db: Session, household_id: int, start: date, end: date) -> tuple[float, float]:
    amount, savings = db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0.0), func.coalesce(func.sum(Expense.savings), 0.0)).where(
            Expense.household_id == household_id, Expense.day >= start, Expense.day <= end
        )
    ).one()
    return float(amount), float(savings)


def _planned(db: Session, household_id: int, start: date, end: date) -> tuple[float, float]:
    """Costs of shopping lists not yet bought, for plans overlapping this period.

    A list is counted in full once, against the period it starts in, to avoid counting a trip
    twice when a plan spans a month boundary.
    """
    rows = db.execute(
        select(ShoppingList.summary)
        .join(MealPlan, MealPlan.id == ShoppingList.plan_id)
        .where(
            MealPlan.household_id == household_id,
            MealPlan.start_date >= start,
            MealPlan.start_date <= end,
            ShoppingList.completed_at.is_(None),
        )
    ).all()
    total = sum((row[0] or {}).get("total", 0) for row in rows)
    savings = sum((row[0] or {}).get("savings", 0) for row in rows)
    return round(total, 2), round(savings, 2)


def _period(start: date, end: date, budget: float | None, db: Session, household_id: int) -> dict:
    spent, saved = _spent(db, household_id, start, end)
    planned, planned_savings = _planned(db, household_id, start, end)
    expected = round(spent + planned, 2)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "budget": budget,
        "spent": round(spent, 2),
        "planned": planned,
        "expected": expected,
        "savings": round(saved + planned_savings, 2),
        "over_budget": bool(budget) and expected > budget,
        "remaining": round(budget - expected, 2) if budget else None,
    }


def dashboard(db: Session, household: Household, user=None, today: date | None = None) -> dict:
    today = today or local_today()
    hs = household_settings(household.settings)
    weekly_budget = hs.budget_weekly
    if user is not None:
        personal = user_settings(user.settings).budget_weekly
        if personal is not None:
            weekly_budget = personal

    monday = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    month_end = today.replace(day=calendar.monthrange(today.year, today.month)[1])

    return {
        "week": _period(monday, monday + timedelta(days=6), weekly_budget, db, household.id),
        "month": _period(month_start, month_end, hs.budget_monthly, db, household.id),
        "economy": economy(db, household.id, today),
    }


def economy(db: Session, household_id: int, today: date | None = None) -> dict:
    """Year to date against the same days last year, for "we spend 28% less than last year".

    Returns no percentage until there is spending in both periods.
    """
    today = today or local_today()
    this_start = today.replace(month=1, day=1)
    last_start = this_start.replace(year=today.year - 1)
    try:
        last_end = today.replace(year=today.year - 1)
    except ValueError:  # 29 February
        last_end = today.replace(year=today.year - 1, day=28)

    this_year, this_saved = _spent(db, household_id, this_start, today)
    last_year, _ = _spent(db, household_id, last_start, last_end)
    change = round((this_year - last_year) / last_year * 100, 1) if last_year > 0 and this_year > 0 else None

    months = []
    for offset in range(11, -1, -1):
        year = today.year + (today.month - 1 - offset) // 12
        month = (today.month - 1 - offset) % 12 + 1
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        amount, saved = _spent(db, household_id, start, end)
        months.append({"month": start.isoformat()[:7], "spent": round(amount, 2), "saved": round(saved, 2)})

    return {
        "this_year_to_date": round(this_year, 2),
        "last_year_same_period": round(last_year, 2),
        "change_percent": change,
        "saved_this_year": round(this_saved, 2),
        "months": months,
    }
