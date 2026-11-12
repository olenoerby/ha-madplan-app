"""Yes and no to individual meals, by one person or a whole family."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Household, MealPlan, MealVote, PlannedMeal, PlanShare, User
from app.services.leftovers import FollowsAnotherMeal, follow
from app.services.pantry import restore_meal
from app.services.shopping import build_shopping_list
from app.settings_schema import household_settings, user_settings


def can_view_plan(db: Session, plan: MealPlan, user: User) -> bool:
    if plan.household_id == user.household_id:
        return True
    return db.get(PlanShare, (plan.id, user.id)) is not None


def can_vote(db: Session, plan: MealPlan, user: User) -> bool:
    if not user_settings(user.settings).features.voting:
        return False
    if plan.household_id == user.household_id:
        return True
    share = db.get(PlanShare, (plan.id, user.id))
    return bool(share and share.can_vote)


def eligible_voters(db: Session, plan: MealPlan) -> list[int]:
    members = db.scalars(select(User).where(User.household_id == plan.household_id, User.is_active.is_(True)))
    voters = [m.id for m in members if user_settings(m.settings).features.voting]
    shares = db.scalars(select(PlanShare).where(PlanShare.plan_id == plan.id, PlanShare.can_vote.is_(True)))
    voters += [s.user_id for s in shares if s.user_id not in voters]
    return voters


def decide(votes: dict[int, str], voters: list[int], rule: str, latest: str) -> str:
    """The meal's status given the votes so far. "pending" means still undecided."""
    yes = sum(1 for v in votes.values() if v == "yes")
    no = sum(1 for v in votes.values() if v == "no")
    total = max(len(voters), 1)
    if rule == "single":
        return "accepted" if latest == "yes" else "rejected"
    if rule == "unanimous":
        if no:
            return "rejected"
        return "accepted" if yes >= total else "pending"
    # Majority of everyone who may vote. A tie, once everyone has voted, counts as no.
    if yes * 2 > total:
        return "accepted"
    if no * 2 >= total:
        return "rejected"
    return "pending"


class MealBought(Exception):
    """Everything for this meal is bought. It can be moved to another day, but not voted off."""


def ensure_can_drop(meal: PlannedMeal) -> None:
    if meal.bought == "full":
        raise MealBought("meal_bought")


def cast_vote(db: Session, meal: PlannedMeal, user: User, vote: str, comment: str | None = None) -> PlannedMeal:
    if meal.leftover_of is not None:
        raise FollowsAnotherMeal("leftovers_follow")
    if vote == "no":
        ensure_can_drop(meal)
    plan = meal.plan
    household = household_settings(db.get(Household, plan.household_id).settings)
    existing = db.get(MealVote, (meal.id, user.id))
    if existing is None:
        db.add(MealVote(meal_id=meal.id, user_id=user.id, vote=vote, comment=comment))
    else:
        existing.vote = vote
        existing.comment = comment
    db.flush()
    db.refresh(meal, attribute_names=["votes"])

    status = decide({v.user_id: v.vote for v in meal.votes}, eligible_voters(db, plan), household.voting_rule, vote)
    change_status(db, meal, status, user)
    return meal


def change_status(db: Session, meal: PlannedMeal, status: str, user: User | None) -> None:
    previous = meal.status
    if previous == status:
        return
    meal.status = status
    follow(db, meal)
    # Nothing goes to Mealie from here. That waits until someone presses "Save and send to Mealie".
    if meal.sync_status == "failed":
        meal.sync_status, meal.sync_error, meal.sync_attempts = "none", None, 0
    if status == "rejected":
        restore_meal(db, meal)
    # A rejected meal leaves the shopping list, and an accepted one might come back onto it.
    if status == "rejected" or previous == "rejected":
        build_shopping_list(db, meal.plan, user)
    db.flush()
