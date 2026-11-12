"""MadPlan as a Home Assistant app: people, roles, the configuration and the pages."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import ha
from app.core.config import settings
from app.main import app
from app.models import User

from .conftest import ingress_headers, login_as, make_user, set_options

# People


def test_someone_opening_the_app_gets_a_user(admin_client, db, home_assistant):
    home_assistant.users.append({"id": "ha-sofie", "group_ids": ["system-users"]})
    client = TestClient(app)
    client.headers.update(ingress_headers("ha-sofie", "sofie", "Sofie Hansen"))
    me = client.get("/api/v1/auth/me").json()
    assert (me["username"], me["display_name"], me["role"], me["locale"]) == ("sofie", "Sofie Hansen", "user", "da")
    assert db.scalar(select(User).where(User.ha_user_id == "ha-sofie")) is not None
    # The same person the next time.
    assert client.get("/api/v1/auth/me").json()["id"] == me["id"]


def test_without_home_assistant_nobody_gets_in(client):
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/plans").status_code == 401


def test_only_the_supervisor_may_say_who_someone_is(monkeypatch, client):
    monkeypatch.setattr(settings, "trust_ingress_headers_from_anywhere", False)
    client.headers.update(ingress_headers("ha-admin", "admin"))
    assert client.get("/api/v1/auth/me").status_code == 401
    monkeypatch.setattr(settings, "ingress_proxy_ip", "testclient")
    assert client.get("/api/v1/auth/me").status_code == 200


def test_the_admin_role_follows_home_assistant(db, home_assistant):
    make_user(db, "bo")
    bo = login_as("bo")
    assert bo.get("/api/v1/auth/me").json()["role"] == "user"
    next(u for u in home_assistant.users if u["id"] == "ha-bo")["group_ids"] = ["system-admin"]
    ha.forget_cached_answers()
    assert bo.get("/api/v1/auth/me").json()["role"] == "admin"
    next(u for u in home_assistant.users if u["id"] == "ha-bo")["group_ids"] = ["system-users"]
    ha.forget_cached_answers()
    assert bo.get("/api/v1/auth/me").json()["role"] == "user"


def test_a_role_stays_when_home_assistant_cannot_be_asked(db):
    make_user(db, "bo", role="admin")

    def down():
        raise ConnectionError("no answer")

    ha.list_users = down
    ha.forget_cached_answers()
    assert login_as("bo").get("/api/v1/auth/me").json()["role"] == "admin"


def test_a_personal_token_still_works_for_an_integration(admin_client, client):
    token = admin_client.post("/api/v1/me/tokens", json={"name": "Home Assistant"}).json()["token"]
    other = TestClient(app)
    assert other.get("/api/v1/ha/summary", headers={"Authorization": f"Bearer {token}"}).status_code == 200


# Roles of MadPlan's own


def test_an_admin_gives_the_roles_that_are_madplans_own(admin_client, db):
    bo = make_user(db, "bo")
    for role in ("household_manager", "guest", "user"):
        assert admin_client.patch(f"/api/v1/admin/users/{bo.id}", json={"role": role}).json()["role"] == role
    assert admin_client.patch(f"/api/v1/admin/users/{bo.id}", json={"role": "admin"}).status_code == 422
    admin_id = db.scalar(select(User.id).where(User.username == "admin"))
    assert admin_client.patch(f"/api/v1/admin/users/{admin_id}", json={"role": "user"}).status_code == 409
    assert login_as("bo").get("/api/v1/admin/users").status_code == 403


def test_a_household_manager_runs_the_household_but_not_the_app(admin_client, db):
    make_user(db, "leder", role="household_manager")
    manager = login_as("leder")
    household = manager.get("/api/v1/household").json()
    assert household["can_edit"] is True
    assert manager.put("/api/v1/household", json={"settings": {**household["settings"], "budget_weekly": 1200}}).status_code == 200
    assert manager.get("/api/v1/admin/setup").status_code == 403


def test_a_guest_looks_and_votes_and_changes_nothing_else(admin_client, db):
    make_user(db, "gaest", role="guest")
    plan = admin_client.post("/api/v1/plans", json={"mode": "days_ahead", "days_ahead": 1, "use_ai": False}).json()
    guest = login_as("gaest")
    seen = guest.get(f"/api/v1/plans/{plan['id']}").json()
    assert seen["is_member"] is False and seen["can_vote"] is True
    assert guest.post(f"/api/v1/meals/{plan['meals'][0]['id']}/vote", json={"vote": "yes"}).status_code == 200
    refused = [
        guest.post("/api/v1/plans", json={"mode": "days_ahead", "days_ahead": 1, "use_ai": False}),
        guest.post("/api/v1/pantry", json={"name": "Mælk"}),
        guest.delete(f"/api/v1/plans/{plan['id']}"),
        guest.post("/api/v1/assistant", json={"message": "hej"}),
    ]
    assert [r.status_code for r in refused] == [403] * len(refused)
    assert guest.post("/api/v1/notifications/read-all").status_code == 200


# The configuration in Home Assistant


def test_the_configuration_is_read_from_the_file_the_supervisor_writes(tmp_path, monkeypatch):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"mealie_url": "http://mealie:9000", "mealie_token": "", "ai_task_entity": "ai_task.x", "auto_plan_weekday": "monday"}))
    monkeypatch.setattr(settings, "options_file", str(path))
    ha.set_options_for_tests(None)
    ha.forget_cached_answers()
    options = ha.options()
    # An empty field counts as not set, so Mealie is not set up without its token.
    assert (options.mealie_url, options.mealie_token, options.mealie_configured) == ("http://mealie:9000", None, False)
    assert (options.ai_task_entity, options.auto_plan_weekday) == ("ai_task.x", "monday")


def test_the_admin_sees_the_setup_but_never_the_token(admin_client, home_assistant):
    set_options(mealie_url="http://mealie:9000", mealie_token="hemmelig", ai_task_entity="ai_task.openai")
    home_assistant.states = [
        {"entity_id": "ai_task.openai", "attributes": {"friendly_name": "OpenAI AI Task"}},
        {"entity_id": "light.kitchen", "attributes": {}},
    ]
    setup = admin_client.get("/api/v1/admin/setup").json()
    assert "hemmelig" not in json.dumps(setup)
    assert setup["mealie"]["configured"] is True and setup["mealie"]["token_set"] is True
    assert setup["ai"] == {"entity": "ai_task.openai", "configured": True, "found": True, "available": [{"entity_id": "ai_task.openai", "name": "OpenAI AI Task"}]}


def test_the_pages_know_whether_mealie_and_ai_are_set_up(admin_client):
    assert admin_client.get("/api/v1/auth/me").json()["setup"] == {"mealie": False, "ai": False}
    set_options(mealie_url="http://mealie:9000", mealie_token="t")
    assert admin_client.get("/api/v1/auth/me").json()["setup"] == {"mealie": True, "ai": False}


def test_messages_go_to_the_chosen_notify_service(admin_client, db, home_assistant):
    from app.services.notifications import notify

    set_options(notify_service="notify.mobile_app_telefon")
    notify(db, db.scalar(select(User).where(User.username == "admin")), "plan_ready", {"start": "2026-11-16", "end": "2026-11-22"})
    db.commit()
    assert home_assistant.calls[-1][0] == "mobile_app_telefon"
    assert home_assistant.calls[-1][1]["title"] == "Madplanen er klar"


def test_each_person_can_send_their_messages_to_their_own_phone(admin_client, db, home_assistant):
    from app.services.notifications import notify

    set_options(notify_service="notify.mobile_app_telefon")
    make_user(db, "mor")
    mor = login_as("mor")
    services = mor.get("/api/v1/me/notify-services").json()
    # Only places to send to, and what is used when nothing is chosen.
    assert [s["service"] for s in services["available"]] == ["notify.mobile_app_mor", "notify.mobile_app_telefon"]
    assert services["default"] == "notify.mobile_app_telefon"
    settings = mor.get("/api/v1/me/settings").json()
    assert mor.put("/api/v1/me/settings", json={**settings, "notify_service": "notify.mobile_app_mor"}).status_code == 200
    # Not a service name: refused.
    assert mor.put("/api/v1/me/settings", json={**settings, "notify_service": "light.turn_on; x"}).status_code == 422

    notify(db, db.scalar(select(User).where(User.username == "mor")), "plan_ready", {"start": "2026-11-16", "end": "2026-11-22"})
    notify(db, db.scalar(select(User).where(User.username == "admin")), "plan_ready", {"start": "2026-11-16", "end": "2026-11-22"})
    db.commit()
    assert [call[0] for call in home_assistant.calls[-2:]] == ["mobile_app_mor", "mobile_app_telefon"]


# The pages


@pytest.fixture
def built_pages(tmp_path, monkeypatch):
    from app import main

    (tmp_path / "plan").mkdir()
    (tmp_path / "_next").mkdir()
    (tmp_path / "index.html").write_text('<script src="/__madplan_base__/_next/app.js"></script>')
    (tmp_path / "plan" / "index.html").write_text('<a href="/__madplan_base__/plan/">Plan</a>')
    (tmp_path / "_next" / "app.js").write_text('fetch("/__madplan_base__/api/v1/plans")')
    (tmp_path.parent / "secret.txt").write_text("hemmeligt")
    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)
    main._rewritten.cache_clear()
    return tmp_path


def test_the_pages_get_home_assistants_ingress_path(client, built_pages):
    headers = {"X-Ingress-Path": "/api/hassio_ingress/abc123"}
    assert client.get("/", headers=headers).text == '<script src="/api/hassio_ingress/abc123/_next/app.js"></script>'
    assert client.get("/plan/", headers=headers).text == '<a href="/api/hassio_ingress/abc123/plan/">Plan</a>'
    assert "/api/hassio_ingress/abc123/api/v1/plans" in client.get("/_next/app.js", headers=headers).text
    # A page that does not exist is the app's own, which shows what fits.
    assert client.get("/no-such-page", headers=headers).text.startswith("<script")


def test_a_strange_ingress_path_is_not_put_into_the_pages(client, built_pages):
    page = client.get("/", headers={"X-Ingress-Path": '"><script>alert(1)</script>'}).text
    assert "alert" not in page and "/_next/app.js" in page


def test_nothing_outside_the_pages_is_served(client, built_pages):
    assert "hemmeligt" not in client.get("/../secret.txt").text
    assert "hemmeligt" not in client.get("/%2e%2e/secret.txt").text
    assert client.get("/api/v1/nothing").status_code == 404
