"""Everything MadPlan asks of Home Assistant, as an app running under its Supervisor.

- The app's configuration, as the administrator set it in Home Assistant (``options``).
- Home Assistant's language, so the app speaks Danish when Home Assistant does.
- Who is an administrator in Home Assistant, as MadPlan's admin role follows it.
- The AI Task entities, which do all of MadPlan's AI work: the agent, its model, its tokens
  and its limits are chosen and managed in Home Assistant, not here.
- Notifications, through one of Home Assistant's notify services.

All calls go through the Supervisor's proxy with the token it gives the app. There is no
other way in, and no token is ever stored by MadPlan.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from app.core.config import settings

log = logging.getLogger(__name__)

Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAYS: tuple[str, ...] = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class AppOptions(BaseModel):
    """The app's configuration in Home Assistant. Mirrors ``options``/``schema`` in config.yaml."""

    mealie_url: str | None = None
    # The address people open Mealie on, for links, when MadPlan reaches it on another one.
    mealie_public_url: str | None = None
    mealie_token: str | None = None
    # The list in Mealie that MadPlan sends to and reads ticks back from.
    mealie_shopping_list: str = "MadPlan"
    mealie_sync_minutes: int = Field(default=15, ge=1, le=1440)
    ai_task_entity: str | None = None
    # Such as "mobile_app_my_phone", without the "notify." in front.
    notify_service: str | None = None
    # Lets the assistant read entity states to answer things like "are we out of toilet paper?".
    assistant_reads_states: bool = False
    crawl_interval_hours: int = Field(default=24, ge=6, le=168)
    buy_signals: bool = True
    recipe_search: bool = True
    auto_plan: bool = False
    auto_plan_weekday: Weekday = "thursday"
    auto_plan_hour: int = Field(default=7, ge=0, le=23)
    chat_retention_days: int = Field(default=180, ge=0, le=3650)
    notification_retention_days: int = Field(default=90, ge=0, le=3650)

    @property
    def mealie_configured(self) -> bool:
        return bool(self.mealie_url and self.mealie_token)

    @property
    def ai_configured(self) -> bool:
        return bool(self.ai_task_entity)


_options_cache: tuple[float, AppOptions] | None = None
_options_override: AppOptions | None = None


def options() -> AppOptions:
    """The current configuration. Read again when the file changes, as the Supervisor rewrites it."""
    global _options_cache
    if _options_override is not None:
        return _options_override
    path = settings.options_path
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return AppOptions()
    if _options_cache and _options_cache[0] == mtime:
        return _options_cache[1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        # The Supervisor writes empty strings for optional fields left blank.
        loaded = AppOptions.model_validate({k: v for k, v in raw.items() if v not in ("", None)})
    except (OSError, ValueError) as exc:
        log.warning("could not read the app configuration: %s", exc)
        loaded = AppOptions()
    _options_cache = (mtime, loaded)
    return loaded


def set_options_for_tests(value: AppOptions | None) -> None:
    global _options_override
    _options_override = value


class HomeAssistantUnavailable(Exception):
    pass


# Tests swap this for a transport that answers as Home Assistant would.
transport: httpx.BaseTransport | None = None


def _client(timeout: float = 30) -> httpx.Client:
    if not settings.token and transport is None:
        raise HomeAssistantUnavailable("no_supervisor_token")
    return httpx.Client(
        base_url=settings.ha_core_url.rstrip("/"),
        headers={"Authorization": f"Bearer {settings.token or 'test'}"},
        timeout=timeout,
        transport=transport,
    )


def core_request(method: str, path: str, timeout: float = 30, **kwargs: Any) -> Any:
    try:
        with _client(timeout) as client:
            response = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise HomeAssistantUnavailable(str(exc)) from exc
    if response.status_code >= 400:
        raise HomeAssistantUnavailable(f"{method} {path} answered {response.status_code}: {response.text[:200]}")
    return response.json() if response.content else None


_config_cache: tuple[float, dict] | None = None


def ha_config() -> dict:
    """Home Assistant's own configuration: language, location name and so on. Kept for a minute."""
    global _config_cache
    if _config_cache and time.monotonic() - _config_cache[0] < 60:
        return _config_cache[1]
    try:
        config = core_request("GET", "/api/config") or {}
    except HomeAssistantUnavailable:
        config = {}
    _config_cache = (time.monotonic(), config)
    return config


def app_language(language: str | None) -> str:
    """MadPlan's language for one of Home Assistant's: Danish for Danish, English for the rest."""
    return "da" if str(language or "").lower().startswith("da") else "en"


def ha_language() -> str:
    """Home Assistant's system language, for when a person's own is not known."""
    return app_language(ha_config().get("language"))


def _websocket_command(command: dict) -> Any:
    """One command over Home Assistant's websocket, for what its REST API does not offer."""
    from websockets.sync.client import connect

    url = settings.ha_core_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/websocket"
    with connect(url, open_timeout=10, close_timeout=2) as socket:
        json.loads(socket.recv(timeout=10))  # auth_required
        socket.send(json.dumps({"type": "auth", "access_token": settings.token}))
        if json.loads(socket.recv(timeout=10)).get("type") != "auth_ok":
            raise HomeAssistantUnavailable("auth_failed")
        socket.send(json.dumps({"id": 1, **command}))
        answer = json.loads(socket.recv(timeout=20))
    if not answer.get("success"):
        raise HomeAssistantUnavailable(str(answer.get("error")))
    return answer.get("result")


# Tests replace this with a function returning the users they need.
list_users = None
_admins_cache: tuple[float, set[str]] | None = None
_admins_lock = threading.Lock()


def ha_admin_ids() -> set[str] | None:
    """The ids of Home Assistant's administrators and owner. None when it cannot be told."""
    global _admins_cache
    with _admins_lock:
        if _admins_cache and time.monotonic() - _admins_cache[0] < 60:
            return _admins_cache[1]
        try:
            users = list_users() if list_users else _websocket_command({"type": "config/auth/list"})
        except Exception as exc:  # noqa: BLE001 (any failure means "cannot tell")
            log.warning("could not read Home Assistant's users: %s", exc)
            return _admins_cache[1] if _admins_cache else None
        admins = {u["id"] for u in users or [] if u.get("is_owner") or "system-admin" in (u.get("group_ids") or [])}
        _admins_cache = (time.monotonic(), admins)
        return admins


def forget_cached_answers() -> None:
    global _admins_cache, _config_cache, _options_cache
    _admins_cache = _config_cache = _options_cache = None


def ai_task_entities() -> list[dict]:
    """The AI Task entities Home Assistant has, for the admin page to show which can be used."""
    try:
        states = core_request("GET", "/api/states") or []
    except HomeAssistantUnavailable:
        return []
    return [
        {"entity_id": s["entity_id"], "name": (s.get("attributes") or {}).get("friendly_name") or s["entity_id"]}
        for s in states
        if str(s.get("entity_id", "")).startswith("ai_task.")
    ]


def generate_data(entity_id: str, task_name: str, instructions: str, attachments: list[dict] | None = None) -> str:
    """Runs one task on Home Assistant's AI Task entity, and returns the text it answered."""
    body: dict[str, Any] = {"entity_id": entity_id, "task_name": task_name, "instructions": instructions}
    if attachments:
        body["attachments"] = attachments
    # AI answers with web searches can take a while.
    answer = core_request("POST", "/api/services/ai_task/generate_data?return_response", json=body, timeout=180)
    response = (answer or {}).get("service_response") or {}
    data = response.get("data")
    if data is None:
        raise HomeAssistantUnavailable("no_data_in_answer")
    return data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)


# Services in the notify domain that are not a place to send to.
_NOT_A_TARGET = {"send_message", "persistent_notification"}


def notify_services() -> list[dict]:
    """Home Assistant's notify services, such as each phone's, for people to choose their own."""
    try:
        domains = core_request("GET", "/api/services") or []
    except HomeAssistantUnavailable:
        return []
    for domain in domains:
        if domain.get("domain") == "notify":
            names = sorted(n for n in (domain.get("services") or {}) if n not in _NOT_A_TARGET)
            return [{"service": f"notify.{n}", "name": (domain["services"][n] or {}).get("name") or n} for n in names]
    return []


def notify(service: str, title: str, message: str) -> None:
    name = service.removeprefix("notify.")
    core_request("POST", f"/api/services/notify/{name}", json={"title": title, "message": message})


def states() -> list[dict]:
    return core_request("GET", "/api/states") or []
