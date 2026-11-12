"""Settings, as the administrator set them in the app's configuration in Home Assistant.

The names are those the rest of the code has always used. What used to be edited on MadPlan's
admin page now comes from ``app.core.ha.options()``. A few things MadPlan learns by itself, such
as the Mealie group's name for links, are still kept in the database.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core import ha
from app.core.config import settings
from app.integrations.home_assistant import HomeAssistantClient
from app.integrations.mealie import MealieClient
from app.models import AppSetting
from app.settings_schema import AiFeatures, AiSettings, IntegrationSettings, ScheduleSettings


def _load[T: BaseModel](db: Session, key: str, schema: type[T]) -> T:
    row = db.get(AppSetting, key)
    return schema.model_validate(row.value if row else {})


def _save(db: Session, key: str, value: BaseModel) -> None:
    row = db.get(AppSetting, key)
    data = value.model_dump(mode="json")
    if row is None:
        db.add(AppSetting(key=key, value=data))
    else:
        row.value = data
    db.flush()


def get_ai_settings(db: Session) -> AiSettings:
    """On when an AI Task entity is chosen. Its model, tokens and limits are Home Assistant's."""
    options = ha.options()
    stored = _load(db, "ai", AiSettings)
    return stored.model_copy(update={"enabled": options.ai_configured, "entity_id": options.ai_task_entity, "features": AiFeatures()})


def save_ai_settings(db: Session, value: AiSettings) -> None:
    _save(db, "ai", value)


def get_schedule(db: Session) -> ScheduleSettings:
    options = ha.options()
    stored = _load(db, "schedule", ScheduleSettings)
    return stored.model_copy(
        update={
            "crawl_interval_hours": options.crawl_interval_hours,
            "buy_signals_enabled": options.buy_signals,
            "recipe_search_enabled": options.recipe_search,
            "auto_plan_enabled": options.auto_plan,
            "auto_plan_weekday": ha.WEEKDAYS.index(options.auto_plan_weekday),
            "auto_plan_hour": options.auto_plan_hour,
            "mealie_sync_minutes": options.mealie_sync_minutes,
            "chat_retention_days": options.chat_retention_days,
            "notification_retention_days": options.notification_retention_days,
        }
    )


def save_schedule(db: Session, value: ScheduleSettings) -> None:
    _save(db, "schedule", value)


def get_integrations(db: Session, household_id: int) -> IntegrationSettings:
    options = ha.options()
    stored = _load(db, f"integrations:{household_id}", IntegrationSettings)
    return stored.model_copy(
        update={
            "mealie_url": options.mealie_url,
            "mealie_public_url": options.mealie_public_url,
            "ha_notify_service": options.notify_service,
            "ha_read_states": options.assistant_reads_states,
        }
    )


def save_integrations(db: Session, household_id: int, value: IntegrationSettings) -> None:
    _save(db, f"integrations:{household_id}", value)


@dataclass
class MealieConfig:
    url: str
    token: str
    shopping_list_id: str | None
    sync_enabled: bool
    shopping_list_name: str = "MadPlan"


def mealie_config(db: Session, household_id: int) -> MealieConfig | None:
    """None when Mealie is not set up, and then nothing about Mealie is shown or done."""
    options = ha.options()
    if not options.mealie_configured:
        return None
    stored = _load(db, f"integrations:{household_id}", IntegrationSettings)
    return MealieConfig(options.mealie_url, options.mealie_token, stored.mealie_shopping_list_id, True, options.mealie_shopping_list)


def mealie_client(db: Session, household_id: int) -> MealieClient | None:
    config = mealie_config(db, household_id)
    return MealieClient(config.url, config.token) if config else None


def mealie_public_base(db: Session, household_id: int) -> str | None:
    """The address to link people to, which may differ from the one MadPlan reaches Mealie on."""
    options = ha.options()
    url = options.mealie_public_url or options.mealie_url
    return url.rstrip("/") if url and options.mealie_configured else None


def ha_client(db: Session, household_id: int) -> HomeAssistantClient | None:
    """Home Assistant, through the Supervisor, with the token it gives the app."""
    token = settings.token
    return HomeAssistantClient(settings.ha_core_url, token, transport=ha.transport) if token or ha.transport else None
