"""Learns what a household tends to say yes and no to, from its votes."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.models import MealPlan, MealVote, PlannedMeal, User
from app.services.text import words

_STOPWORDS = {
    "med", "og", "i", "på", "til", "af", "en", "et", "the", "with", "and", "hjemmelavet", "bagt",
    "bagte", "stegt", "stegte", "fad", "ovn", "salat", "kartofler", "ris", "brun", "sovs",
}
_HISTORY_DAYS = 365
_MIN_VOTES = 2
_MAX_WEIGHT = 3.0


@dataclass
class LearnedTaste:
    weights: dict[str, float]
    liked: list[dict]
    rejected: list[dict]
    members: list[dict] = field(default_factory=list)


def learn_taste(db: Session, household_id: int) -> LearnedTaste:
    since = utcnow() - timedelta(days=_HISTORY_DAYS)
    rows = db.execute(
        select(PlannedMeal.title, MealVote.vote, MealVote.user_id, User.display_name, User.household_id)
        .join(MealVote, MealVote.meal_id == PlannedMeal.id)
        .join(User, User.id == MealVote.user_id)
        .join(MealPlan, MealPlan.id == PlannedMeal.plan_id)
        .where(MealPlan.household_id == household_id, MealVote.created_at >= since)
    ).all()

    yes: dict[str, int] = defaultdict(int)
    no: dict[str, int] = defaultdict(int)
    people: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    names: dict[int, str] = {}
    for title, vote, user_id, name, voter_household in rows:
        # People a plan was shared with count towards what suits the plan, but what they
        # personally like is not this household's to look at.
        if voter_household == household_id:
            names[user_id] = name
        for word in set(words(title)) - _STOPWORDS:
            if len(word) > 2:
                (yes if vote == "yes" else no)[word] += 1
                people[user_id][word][0 if vote == "yes" else 1] += 1

    weights: dict[str, float] = {}
    liked: list[dict] = []
    rejected: list[dict] = []
    for word in set(yes) | set(no):
        y, n = yes[word], no[word]
        if y + n < _MIN_VOTES:
            continue
        # Each person counts once, so that the one who votes on everything does not outweigh
        # the one who only speaks up about fish.
        leanings = [_leaning(*counts[word]) for counts in people.values() if word in counts]
        weights[word] = sum(leanings) / len(leanings)
        entry = {"term": word, "yes": y, "no": n}
        balance = (y - n) / (y + n)
        if balance > 0.2:
            liked.append(entry)
        elif balance < -0.2:
            rejected.append(entry)
    return LearnedTaste(
        weights=weights,
        liked=_top(liked),
        rejected=_top(rejected),
        members=[_member_taste(names[uid], people[uid]) for uid in sorted(names, key=lambda u: names[u].lower())],
    )


def _leaning(y: int, n: int) -> float:
    return (y - n) / (y + n) * min(_MAX_WEIGHT, float(y + n))


def _top(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda e: (-abs(e["yes"] - e["no"]), e["term"]))[:10]


def _member_taste(name: str, counts: dict[str, list[int]]) -> dict:
    liked: list[dict] = []
    rejected: list[dict] = []
    for word, (y, n) in counts.items():
        if y + n < _MIN_VOTES:
            continue
        entry = {"term": word, "yes": y, "no": n}
        balance = (y - n) / (y + n)
        if balance > 0.2:
            liked.append(entry)
        elif balance < -0.2:
            rejected.append(entry)
    return {"name": name, "liked": _top(liked)[:5], "rejected": _top(rejected)[:5]}
