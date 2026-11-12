"""A person's right to see what is kept about them, and to have it removed.

MadPlan runs on someone's own server, but the household's eating habits are still personal data,
so each member can take their data with them or leave for good. Passwords and token values are
never part of an export. What belongs to the household, like its plans, stays when one person
leaves, without their name on it.
"""

from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.models import (
    AiUsage,
    ApiToken,
    AuthIdentity,
    ChatMessage,
    Expense,
    Household,
    MealAlternative,
    MealPlan,
    MealVote,
    Notification,
    PantryItem,
    PlannedMeal,
    PlanShare,
    PriceAlert,
    ShoppingList,
    TrackedItem,
    User,
    UserSession,
    UserStore,
)


class LastAdmin(ValueError):
    """Someone has to be left who can run the place."""


def _household_data(db: Session, household_id: int) -> dict:
    household = db.get(Household, household_id)
    plans = db.scalars(select(MealPlan).where(MealPlan.household_id == household_id).order_by(MealPlan.start_date))
    lists = db.scalars(select(ShoppingList).where(ShoppingList.household_id == household_id).order_by(ShoppingList.id))
    pantry = db.scalars(select(PantryItem).where(PantryItem.household_id == household_id).order_by(PantryItem.name))
    expenses = db.scalars(select(Expense).where(Expense.household_id == household_id).order_by(Expense.day))
    return {
        "name": household.name,
        "settings": household.settings,
        "members": [m.display_name for m in db.scalars(select(User).where(User.household_id == household_id))],
        "pantry": [
            {
                "name": i.name,
                "quantity": i.quantity,
                "unit": i.unit,
                "min_quantity": i.min_quantity,
                "location": i.location,
                "expires_on": i.expires_on,
            }
            for i in pantry
        ],
        "plans": [
            {
                "start": p.start_date,
                "end": p.end_date,
                "meals": [
                    {"date": m.date, "meal_type": m.meal_type, "title": m.title, "status": m.status, "cooked": m.cooked}
                    for m in p.meals
                ],
            }
            for p in plans
        ],
        "shopping_lists": [
            {
                "name": s.name,
                "completed_at": s.completed_at,
                "items": [{"name": i.name, "quantity": i.quantity, "unit": i.unit, "cost": i.cost, "checked": i.checked} for i in s.items],
            }
            for s in lists
        ],
        "expenses": [{"day": e.day, "amount": e.amount, "savings": e.savings, "note": e.note} for e in expenses],
    }


def export_user_data(db: Session, user: User) -> dict:
    votes = db.execute(
        select(MealVote, PlannedMeal)
        .join(PlannedMeal, PlannedMeal.id == MealVote.meal_id)
        .where(MealVote.user_id == user.id)
        .order_by(PlannedMeal.date)
    ).all()
    return {
        "exported_at": utcnow(),
        "profile": {
            "username": user.username,
            "display_name": user.display_name,
            "email": user.email,
            "role": user.role,
            "locale": user.locale,
            "created_at": user.created_at,
            "last_login_at": user.last_login_at,
            "settings": user.settings,
        },
        "login_methods": [{"provider": i.provider, "subject": i.subject, "created_at": i.created_at, "last_used_at": i.last_used_at} for i in user.identities],
        "api_tokens": [
            {"name": t.name, "created_at": t.created_at, "last_used_at": t.last_used_at}
            for t in db.scalars(select(ApiToken).where(ApiToken.user_id == user.id))
        ],
        "tracked_items": [{"query": t.query, "priority": t.priority} for t in db.scalars(select(TrackedItem).where(TrackedItem.user_id == user.id))],
        "price_alerts": [
            {"query": a.query, "max_price": a.max_price, "basis": a.basis, "active": a.active, "created_at": a.created_at}
            for a in db.scalars(select(PriceAlert).where(PriceAlert.user_id == user.id))
        ],
        "votes": [{"date": meal.date, "meal": meal.title, "vote": vote.vote, "comment": vote.comment} for vote, meal in votes],
        "notifications": [
            {"kind": n.kind, "data": n.data, "created_at": n.created_at}
            for n in db.scalars(select(Notification).where(Notification.user_id == user.id).order_by(Notification.id))
        ],
        "assistant_chats": [
            {"role": m.role, "content": m.content, "created_at": m.created_at}
            for m in db.scalars(select(ChatMessage).where(ChatMessage.user_id == user.id).order_by(ChatMessage.id))
        ],
        "household": _household_data(db, user.household_id),
    }


def delete_user(db: Session, user: User) -> None:
    """Removes a person. Written out, as SQLite only cascades when told to, and tests run on it."""
    # Administrators are Home Assistant's, so there is always one to be had.
    for model in (UserSession, ApiToken, TrackedItem, PriceAlert, Notification, ChatMessage, UserStore, MealVote, PlanShare, AuthIdentity):
        db.execute(delete(model).where(model.user_id == user.id))
    db.execute(update(MealPlan).where(MealPlan.created_by == user.id).values(created_by=None))
    db.execute(update(PlannedMeal).where(PlannedMeal.added_by == user.id).values(added_by=None))
    db.execute(update(MealAlternative).where(MealAlternative.proposed_by == user.id).values(proposed_by=None))
    db.execute(update(Expense).where(Expense.user_id == user.id).values(user_id=None))
    db.execute(update(AiUsage).where(AiUsage.user_id == user.id).values(user_id=None))
    db.expire(user)
    db.delete(user)
    db.flush()
