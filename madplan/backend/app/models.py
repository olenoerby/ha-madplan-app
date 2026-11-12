"""Database schema.

Settings that are read and written as a whole live in JSON columns validated by the
Pydantic models in ``app.settings_schema``. Anything that is queried or joined on has
its own column.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import expression

from app.core.db import Base, UTCDateTime, utcnow

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Household(Base):
    __tablename__ = "households"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    members: Mapped[list[User]] = relationship(back_populates="household")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    # The Home Assistant user this is. Everyone comes in through Home Assistant's login.
    ha_user_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="user")
    locale: Mapped[str] = mapped_column(String(5), default="da")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    household: Mapped[Household] = relationship(back_populates="members")
    identities: Mapped[list[AuthIdentity]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def manages_household(self) -> bool:
        """May change the household's own settings: an admin, or the household's manager."""
        return self.role in ("admin", "household_manager")

    @property
    def is_guest(self) -> bool:
        """May look and vote, and change nothing but their own account."""
        return self.role == "guest"


class AuthIdentity(Base):
    """One login method. A user can hold any number of them.

    ``provider`` is ``local`` (subject is the username, with a password hash), or ``google``
    or ``microsoft`` (subject is the verified e-mail from that method's Access application).
    The same address under the two is two identities, which can belong to two people.
    ``cloudflare`` is an identity from before the two were told apart. Its first use settles it.
    """

    __tablename__ = "auth_identities"
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_identity_provider_subject"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    subject: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    user: Mapped[User] = relationship(back_populates="identities")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ApiToken(Base):
    """Long-lived bearer tokens, used by the Home Assistant integration."""

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    default_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Stores on the requested list are "known"; anything else a crawler finds (Matas,
    # border shops) is kept for price alerts but stays out of meal planning.
    is_known_grocery: Mapped[bool] = mapped_column(Boolean, default=False)


class HouseholdStore(Base):
    __tablename__ = "household_stores"

    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), primary_key=True
    )
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    branch_name: Mapped[str | None] = mapped_column(String(160))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    distance_km: Mapped[float | None] = mapped_column(Float)


class UserStore(Base):
    """A user's own override of the household's store choice. NULL means inherit."""

    __tablename__ = "user_stores"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool | None] = mapped_column(Boolean)
    favorite: Mapped[bool | None] = mapped_column(Boolean)


class CrawlSource(Base):
    __tablename__ = "crawl_sources"

    slug: Mapped[str] = mapped_column(String(40), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_status: Mapped[str | None] = mapped_column(String(40))


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(20), default="running")
    terms: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    denied_by_policy: Mapped[int] = mapped_column(Integer, default=0)
    offers_seen: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class Offer(Base):
    __tablename__ = "offers"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_offer_source_external"),
        Index("ix_offers_validity", "valid_from", "valid_to"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    external_id: Mapped[str] = mapped_column(String(80))
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    search_text: Mapped[str] = mapped_column(Text)
    # "offer" is a time-limited deal, "shelf" a regular price without a validity period.
    kind: Mapped[str] = mapped_column(String(10), default="offer")
    price: Mapped[float] = mapped_column(Float)
    pre_price: Mapped[float | None] = mapped_column(Float)
    pack_quantity: Mapped[float | None] = mapped_column(Float)
    pack_unit: Mapped[str | None] = mapped_column(String(8))
    unit_price: Mapped[float | None] = mapped_column(Float)
    unit_basis: Mapped[str | None] = mapped_column(String(8))
    membership_required: Mapped[bool] = mapped_column(Boolean, default=False)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    url: Mapped[str | None] = mapped_column(String(500))
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    store: Mapped[Store] = relationship()


class PricePoint(Base):
    """Price history. Survives after the offer ends and is what the price index reads."""

    __tablename__ = "price_points"
    __table_args__ = (
        UniqueConstraint("source", "external_id", "observed_on", name="uq_price_point_daily"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    external_id: Mapped[str] = mapped_column(String(80))
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    search_text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(10), default="offer")
    price: Mapped[float] = mapped_column(Float)
    unit_price: Mapped[float | None] = mapped_column(Float)
    unit_basis: Mapped[str | None] = mapped_column(String(8))
    observed_on: Mapped[date] = mapped_column(Date, index=True)


class TrackedItem(Base):
    """A user's priority for a product: -3 (unimportant) to +3 (important), 0 by default."""

    __tablename__ = "tracked_items"
    __table_args__ = (UniqueConstraint("user_id", "query", name="uq_tracked_user_query"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    query: Mapped[str] = mapped_column(String(160))
    priority: Mapped[int] = mapped_column(Integer, default=0)


class PriceAlert(Base):
    __tablename__ = "price_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    query: Mapped[str] = mapped_column(String(160))
    max_price: Mapped[float] = mapped_column(Float)
    # "pack" compares the shelf-edge price, "kg"/"l"/"stk" compare the unit price.
    basis: Mapped[str] = mapped_column(String(8), default="pack")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Also search the wider web through the AI (costs money, for things like make-up).
    use_web_search: Mapped[bool] = mapped_column(Boolean, default=False)
    last_triggered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_web_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_match: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_read", "user_id", "read_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(30))
    # Deduplicates repeated work: the same key is only ever notified once.
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class PantryItem(Base):
    __tablename__ = "pantry_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    quantity: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(12))
    location: Mapped[str | None] = mapped_column(String(60))
    expires_on: Mapped[date | None] = mapped_column(Date, index=True)
    # True when the date is MadPlan's guess for something just bought, and not from the package.
    expires_estimated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false())
    # Keep at least this much, in the item's own unit. Below it, MadPlan suggests buying more.
    min_quantity: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(12), default="manual")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Recipe(Base):
    """NULL household_id means a built-in recipe, visible to every household."""

    __tablename__ = "recipes"
    __table_args__ = (Index("ix_recipes_household_source", "household_id", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int | None] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(12), default="builtin")
    title: Mapped[str] = mapped_column(String(200))
    title_en: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    meal_types: Mapped[list[str]] = mapped_column(JSONType, default=list)
    servings: Mapped[float] = mapped_column(Float, default=4)
    ingredients: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    steps: Mapped[list[str]] = mapped_column(JSONType, default=list)
    steps_en: Mapped[list[str] | None] = mapped_column(JSONType)
    tags: Mapped[list[str]] = mapped_column(JSONType, default=list)
    source_url: Mapped[str | None] = mapped_column(String(500))
    # The picture the page itself names. Linked to and never stored, and Mealie keeps its own copy.
    image_url: Mapped[str | None] = mapped_column(String(1000))
    # Someone said no thanks to a recipe found on the web, so it is not suggested again.
    dismissed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    mealie_slug: Mapped[str | None] = mapped_column(String(200))
    mealie_id: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MealPlan(Base):
    __tablename__ = "meal_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    meal_types: Mapped[list[str]] = mapped_column(JSONType, default=list)
    budget: Mapped[float | None] = mapped_column(Float)
    # True when a suggestion from the AI actually reached the candidate pool.
    ai_used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    days: Mapped[list[PlanDay]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="PlanDay.date"
    )
    meals: Mapped[list[PlannedMeal]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="PlannedMeal.date"
    )


class PlanShare(Base):
    """Shares a plan with a user outside the household."""

    __tablename__ = "plan_shares"

    plan_id: Mapped[int] = mapped_column(ForeignKey("meal_plans.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    can_vote: Mapped[bool] = mapped_column(Boolean, default=True)


class PlanDay(Base):
    __tablename__ = "plan_days"

    plan_id: Mapped[int] = mapped_column(ForeignKey("meal_plans.id", ondelete="CASCADE"), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    guest_adults: Mapped[int] = mapped_column(Integer, default=0)
    guest_children: Mapped[int] = mapped_column(Integer, default=0)

    plan: Mapped[MealPlan] = relationship(back_populates="days")


class PlannedMeal(Base):
    __tablename__ = "planned_meals"
    __table_args__ = (Index("ix_planned_meals_plan_date", "plan_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("meal_plans.id", ondelete="CASCADE"))
    date: Mapped[date] = mapped_column(Date)
    meal_type: Mapped[str] = mapped_column(String(12))
    recipe_id: Mapped[int | None] = mapped_column(ForeignKey("recipes.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200))
    adults: Mapped[int] = mapped_column(Integer, default=2)
    children: Mapped[int] = mapped_column(Integer, default=0)
    # pending -> accepted | rejected
    status: Mapped[str] = mapped_column(String(12), default="pending")
    estimated_cost: Mapped[float] = mapped_column(Float, default=0)
    normal_cost: Mapped[float | None] = mapped_column(Float)
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    # none -> pending -> synced | failed
    sync_status: Mapped[str] = mapped_column(String(10), default="none")
    sync_error: Mapped[str | None] = mapped_column(Text)
    sync_attempts: Mapped[int] = mapped_column(Integer, default=0)
    mealie_entry_id: Mapped[str | None] = mapped_column(String(60))
    # The note about guests. A separate entry, as Mealie shows no text on an entry with a recipe.
    mealie_note_id: Mapped[str | None] = mapped_column(String(60))
    # What was last sent, so that sending again can change just what is different.
    mealie_sent: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    # None until someone says. False means it was not made, so it used nothing up.
    cooked: Mapped[bool | None] = mapped_column(Boolean)
    # none | partial | full. Set by people, and to full when the plan's list is shopped. Once
    # everything is bought, the meal can be moved to another day but no longer voted off.
    bought: Mapped[str] = mapped_column(String(8), default="none", server_default="none")
    # Whoever put this dish on the plan by hand. None when the planner chose it.
    added_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Extra adult portions cooked with this meal, to be eaten later as its leftovers.
    leftover_portions: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    # On a leftovers meal: the meal it was cooked with. It has no recipe of its own, so it buys
    # nothing and uses nothing from the pantry, and it follows that meal's decision.
    leftover_of: Mapped[int | None] = mapped_column(ForeignKey("planned_meals.id", ondelete="CASCADE"))
    # When the ingredients left the pantry, and what was taken, so that it can be put back.
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    consumed: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)

    plan: Mapped[MealPlan] = relationship(back_populates="meals")
    recipe: Mapped[Recipe | None] = relationship()
    added_by_user: Mapped[User | None] = relationship(foreign_keys=[added_by])
    votes: Mapped[list[MealVote]] = relationship(back_populates="meal", cascade="all, delete-orphan")
    alternatives: Mapped[list[MealAlternative]] = relationship(
        back_populates="meal", cascade="all, delete-orphan"
    )


class MealVote(Base):
    __tablename__ = "meal_votes"

    meal_id: Mapped[int] = mapped_column(ForeignKey("planned_meals.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    vote: Mapped[str] = mapped_column(String(4))
    comment: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    meal: Mapped[PlannedMeal] = relationship(back_populates="votes")
    user: Mapped[User] = relationship()


class MealAlternative(Base):
    __tablename__ = "meal_alternatives"

    id: Mapped[int] = mapped_column(primary_key=True)
    meal_id: Mapped[int] = mapped_column(ForeignKey("planned_meals.id", ondelete="CASCADE"), index=True)
    proposed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # "user" typed a free-text idea, "system" picked the next best recipe, "web" found a page.
    source: Mapped[str] = mapped_column(String(8), default="user")
    title: Mapped[str] = mapped_column(String(200))
    recipe_id: Mapped[int | None] = mapped_column(ForeignKey("recipes.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    meal: Mapped[PlannedMeal] = relationship(back_populates="alternatives")


class ShoppingList(Base):
    __tablename__ = "shopping_lists"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("meal_plans.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    items: Mapped[list[ShoppingItem]] = relationship(
        back_populates="shopping_list", cascade="all, delete-orphan", order_by="ShoppingItem.id"
    )


class ShoppingItem(Base):
    __tablename__ = "shopping_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[int] = mapped_column(ForeignKey("shopping_lists.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(12))
    packs: Mapped[int] = mapped_column(Integer, default=1)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"))
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("offers.id", ondelete="SET NULL"))
    offer_title: Mapped[str | None] = mapped_column(String(300))
    # offer | shelf | reference (our own estimate) | unknown
    price_source: Mapped[str] = mapped_column(String(10), default="unknown")
    cost: Mapped[float | None] = mapped_column(Float)
    normal_cost: Mapped[float | None] = mapped_column(Float)
    # before (the offer's own) | shelf (what stores usually charge) | estimate (our own guess)
    normal_source: Mapped[str | None] = mapped_column(String(10))
    pack_quantity: Mapped[float | None] = mapped_column(Float)
    covered_by_pantry: Mapped[bool] = mapped_column(Boolean, default=False)
    checked: Mapped[bool] = mapped_column(Boolean, default=False)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)

    shopping_list: Mapped[ShoppingList] = relationship(back_populates="items")
    store: Mapped[Store | None] = relationship()


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (Index("ix_expenses_household_day", "household_id", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    day: Mapped[date] = mapped_column(Date)
    amount: Mapped[float] = mapped_column(Float)
    savings: Mapped[float] = mapped_column(Float, default=0)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"))
    list_id: Mapped[int | None] = mapped_column(ForeignKey("shopping_lists.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(String(200))


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    engine: Mapped[str] = mapped_column(String(10), default="ai")
    # What the assistant offered to do. Nothing happens until the person approves it.
    proposals: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AiUsage(Base):
    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    feature: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(60))
    requests: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    web_searches: Mapped[int] = mapped_column(Integer, default=0)
    cost_dkk: Mapped[float] = mapped_column(Float, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)


class AppSetting(Base):
    """Admin settings, one row per section. Tokens inside are Fernet-encrypted."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
