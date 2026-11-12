"""Typed views of the JSON settings columns, and the rules for how they combine.

Household settings hold the defaults. A user setting left at ``None`` (or an empty list
for the lists that add up) inherits from the household, so every option can be set per
user without forcing anyone to set it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import settings as process_settings

MealType = Literal["breakfast", "lunch", "dinner", "snack", "drink"]
MEAL_TYPES: tuple[str, ...] = ("breakfast", "lunch", "dinner", "snack", "drink")
DietMode = Literal["mediterranean", "vegetarian", "vegan", "keto", "low_carb", "pescetarian"]
VotingRule = Literal["single", "majority", "unanimous"]
Frequency = Literal["rarely", "often", "always"]


class HouseholdSettings(BaseModel):
    default_adults: int = Field(default_factory=lambda: process_settings.default_adults, ge=0, le=30)
    default_children: int = Field(default_factory=lambda: process_settings.default_children, ge=0, le=30)
    child_portion: float = Field(default=0.6, ge=0.1, le=1.0)

    address: str | None = None
    lat: float | None = None
    lon: float | None = None

    budget_weekly: float | None = Field(default=900, ge=0)
    budget_monthly: float | None = Field(default=3600, ge=0)

    default_meal_types: list[MealType] = ["dinner"]
    skip_weekdays: list[int] = []  # 0 = Monday

    # Do not suggest the same dish again within this many days.
    repeat_avoid_days: int = Field(default=21, ge=0, le=365)
    # At most this many meals with the same main protein per week (0 disables the limit).
    repeat_protein_per_week: int = Field(default=3, ge=0, le=14)

    voting_rule: VotingRule = "single"

    allergies: list[str] = []
    dislikes: list[str] = []
    likes: list[str] = []
    # How often a food should come up: "fisk": "often", "pasta": "rarely", "salat": "always".
    frequencies: dict[str, Frequency] = {}
    meat_preferences: list[str] = []
    diet_mode: DietMode | None = None
    diet_coach_enabled: bool = False
    assume_staples: bool = True
    # MadPlan's own dishes have no web page behind them, so they are off unless asked for. For
    # those with neither Mealie nor AI. They can be planned, but never become recipes in Mealie.
    builtin_dishes: bool = False

    max_stores: int = Field(default=2, ge=1, le=5)
    cost_per_km: float = Field(default=3.0, ge=0)
    # Value of the extra trip itself, so a saving of a few kroner is not "worth it".
    cost_per_extra_store: float = Field(default=15.0, ge=0)

    # Sending the home address or position to OpenStreetMap services is opt-in.
    allow_osm_lookup: bool = False
    waste_alert_days: int = Field(default=3, ge=0, le=30)
    # Meals that have been eaten take their ingredients out of the pantry.
    track_consumption: bool = True
    # What is bought gets a best-before date from MadPlan's rough estimate, marked as an estimate.
    estimate_expiry: bool = True
    # "Not now" on a restock suggestion, as ingredient key to the date it may come back.
    restock_snoozed: dict[str, str] = {}
    # Offers someone said are not that ingredient, as "ingredient|offer title".
    blocked_matches: list[str] = []


class FeatureToggles(BaseModel):
    assistant: bool = True
    price_alerts: bool = True
    voting: bool = True
    waste_agent: bool = True
    diet_coach: bool = True
    pantry_photo: bool = True
    ha_notifications: bool = True
    buy_signals: bool = True
    restock: bool = True
    plan_updates: bool = True
    new_recipes: bool = True


class UserSettings(BaseModel):
    allergies: list[str] = []
    dislikes: list[str] = []
    likes: list[str] = []
    frequencies: dict[str, Frequency] = {}
    meat_preferences: list[str] = []
    diet_mode: DietMode | None = None
    diet_coach_enabled: bool | None = None
    budget_weekly: float | None = Field(default=None, ge=0)
    max_price_per_meal: float | None = Field(default=None, ge=0)
    use_phone_location: bool = False
    pantry_photo_auto_apply: bool = True
    features: FeatureToggles = FeatureToggles()


class AiFeatures(BaseModel):
    assistant: bool = True
    recipe_finder: bool = True
    vision: bool = True
    diet_coach: bool = True
    waste_agent: bool = True
    price_hunt: bool = True


class AiSettings(BaseModel):
    enabled: bool = True
    # The Home Assistant AI Task entity that does the work, from the app's configuration.
    entity_id: str | None = None
    model: str = "home-assistant"
    vision_model: str = "home-assistant"
    # Reasoning tokens are billed as output. GPT-5.6 defaults to medium, so this saves money.
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] = "low"
    # Hard stop: no call is started once this month's estimated spend has reached it.
    monthly_budget_dkk: float = Field(default=200, ge=0)
    usd_to_dkk: float = Field(default=7.0, gt=0)
    # USD per million tokens. Editable because prices change; checked 2026-11-03.
    price_input_per_m: float = Field(default=0.20, ge=0)
    price_cached_per_m: float = Field(default=0.02, ge=0)
    price_output_per_m: float = Field(default=1.20, ge=0)
    cache_write_multiplier: float = Field(default=1.25, ge=0)
    price_web_search_per_call: float = Field(default=0.01, ge=0)
    max_turns: int = Field(default=6, ge=1, le=20)
    max_output_tokens: int = Field(default=1200, ge=100, le=8000)
    chat_history_messages: int = Field(default=6, ge=0, le=30)
    plan_ai_candidates: int = Field(default=6, ge=0, le=20)
    features: AiFeatures = AiFeatures()


class ScheduleSettings(BaseModel):
    crawl_enabled: bool = True
    # Leaflets change weekly, so once a day is plenty, and easier on the sites.
    crawl_interval_hours: float = Field(default=24, ge=1)
    # Never below 1 s between requests to the same site, whatever an admin enters.
    crawl_request_delay_seconds: float = Field(default=3.0, ge=1.0)
    crawl_max_terms: int = Field(default=150, ge=1, le=500)
    alert_interval_hours: float = Field(default=6, ge=0.25)
    waste_check_hours: float = Field(default=24, ge=1)
    mealie_sync_minutes: float = Field(default=15, ge=1)
    price_hunt_interval_hours: float = Field(default=48, ge=6)
    auto_plan_enabled: bool = False
    auto_plan_weekday: int = Field(default=3, ge=0, le=6)
    auto_plan_hour: int = Field(default=7, ge=0, le=23)
    restock_check_hours: float = Field(default=24, ge=1)
    buy_signals_enabled: bool = True
    # New offers can change what next week's plan costs, so open lists are priced again.
    reprice_after_crawl: bool = True
    # Looks for recipes around the current offers, and costs a little of the AI budget each time.
    recipe_search_enabled: bool = True
    recipe_search_interval_hours: float = Field(default=168, ge=24)
    # How long old chats and messages are kept. 0 keeps them.
    chat_retention_days: int = Field(default=180, ge=0)
    notification_retention_days: int = Field(default=90, ge=0)


class IntegrationSettings(BaseModel):
    """Stored with tokens encrypted. Environment variables are the fallback."""

    mealie_url: str | None = None
    # The address people use, for links, when MadPlan reaches Mealie on an internal one.
    mealie_public_url: str | None = None
    mealie_group_slug: str | None = None
    mealie_token_enc: str | None = None
    mealie_shopping_list_id: str | None = None
    mealie_sync_enabled: bool = True
    ha_url: str | None = None
    ha_token_enc: str | None = None
    ha_notify_service: str | None = None
    ha_read_states: bool = False
    openai_key_enc: str | None = None


class EffectiveProfile(BaseModel):
    """What the planner actually works with for a household, all members combined."""

    allergies: list[str]
    dislikes: list[str]
    likes: list[str]
    # Each term's wish, from -1 (rarely) to 2 (always), as the average of those who set one.
    frequencies: dict[str, float] = {}
    meat_preferences: list[str]
    diet_mode: DietMode | None
    adults: int
    children: int
    child_portion: float


def _unique(*lists: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for values in lists:
        for value in values:
            cleaned = value.strip()
            if cleaned:
                seen.setdefault(cleaned.lower(), cleaned)
    return list(seen.values())


FREQUENCY_WEIGHT = {"rarely": -1.0, "often": 1.0, "always": 2.0}


def combine_frequencies(household: dict[str, str], members: list[dict[str, str]]) -> dict[str, float]:
    """One wish per person and term, averaged, so one person's "always" does not outweigh another's
    "rarely". The household's own setting counts as one more voice."""
    votes: dict[str, list[float]] = {}
    names: dict[str, str] = {}
    for wishes in [household, *members]:
        for term, frequency in wishes.items():
            cleaned = term.strip()
            if not cleaned or frequency not in FREQUENCY_WEIGHT:
                continue
            key = cleaned.lower()
            names.setdefault(key, cleaned)
            votes.setdefault(key, []).append(FREQUENCY_WEIGHT[frequency])
    return {names[key]: round(sum(values) / len(values), 2) for key, values in votes.items()}


def household_settings(raw: dict | None) -> HouseholdSettings:
    return HouseholdSettings.model_validate(raw or {})


def user_settings(raw: dict | None) -> UserSettings:
    return UserSettings.model_validate(raw or {})


def effective_profile(household_raw: dict | None, member_raws: list[dict | None]) -> EffectiveProfile:
    """Combines a household's defaults with its members.

    Allergies are a union across everyone: one person's allergy keeps the food out for
    all. Likes and dislikes also combine, and only affect ranking. A diet mode set on
    the household wins; otherwise a mode shared by every member who set one applies.
    """
    household = household_settings(household_raw)
    members = [user_settings(raw) for raw in member_raws]
    modes = {m.diet_mode for m in members if m.diet_mode}
    return EffectiveProfile(
        allergies=_unique(household.allergies, *[m.allergies for m in members]),
        dislikes=_unique(household.dislikes, *[m.dislikes for m in members]),
        likes=_unique(household.likes, *[m.likes for m in members]),
        frequencies=combine_frequencies(household.frequencies, [m.frequencies for m in members]),
        meat_preferences=_unique(household.meat_preferences, *[m.meat_preferences for m in members]),
        diet_mode=household.diet_mode or (modes.pop() if len(modes) == 1 else None),
        adults=household.default_adults,
        children=household.default_children,
        child_portion=household.child_portion,
    )
