"""Outbound calls to Home Assistant: push notifications and, when allowed, read entity states.

The other direction, Home Assistant reading MadPlan, is the integration in the
olenoerby/ha-madplan-integration repository, which talks to the ``/api/v1/ha`` routes.
"""

from __future__ import annotations

from typing import Any

import httpx


class HomeAssistantError(Exception):
    pass


class HomeAssistantClient:
    def __init__(self, base_url: str, token: str, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HomeAssistantClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HomeAssistantError(str(exc)) from exc
        return response.json()

    def ping(self) -> bool:
        return bool(self._request("GET", "/api/"))

    def states(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/states")

    def notify(self, service: str, title: str, message: str) -> None:
        """``service`` is the part after notify., such as ``mobile_app_my_phone``."""
        domain_service = service.removeprefix("notify.")
        self._request("POST", f"/api/services/notify/{domain_service}", json={"title": title, "message": message})


def summarize_states(states: list[dict[str, Any]], query: str | None = None, limit: int = 40) -> list[dict[str, str]]:
    """A short, relevant list for the assistant. The full state dump would be costly to send."""
    interesting = ("sensor.", "binary_sensor.", "todo.", "calendar.", "input_number.", "input_boolean.")
    needle = (query or "").lower()
    results = []
    for state in states:
        entity = state.get("entity_id", "")
        if not entity.startswith(interesting):
            continue
        name = (state.get("attributes") or {}).get("friendly_name", entity)
        if needle and needle not in entity.lower() and needle not in str(name).lower():
            continue
        results.append({"entity": entity, "name": str(name), "state": str(state.get("state"))})
        if len(results) >= limit:
            break
    return results
