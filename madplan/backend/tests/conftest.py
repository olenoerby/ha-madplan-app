from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from pathlib import Path

# Must be set before the app is imported. Tests run on SQLite unless told otherwise, and on
# Postgres when MADPLAN_TEST_DATABASE_URL is set.
_tmp = Path(tempfile.mkdtemp(prefix="madplan-tests-"))
_test_db = os.environ.get("MADPLAN_TEST_DATABASE_URL")
os.environ["MADPLAN_DATABASE_URL"] = _test_db or f"sqlite:///{_tmp / 'test.db'}"
os.environ["MADPLAN_DATA_DIR"] = str(_tmp)
os.environ["MADPLAN_MEDIA_DIR"] = str(_tmp / "media")
os.environ["MADPLAN_APP_ENV"] = "development"
# The test client is not the Supervisor, so the ingress headers are trusted from anywhere here.
os.environ["MADPLAN_TRUST_INGRESS_HEADERS_FROM_ANYWHERE"] = "1"
os.environ["MADPLAN_RUN_JOBS"] = "0"
os.environ["MADPLAN_STATIC_DIR"] = str(_tmp / "static")
os.environ.pop("SUPERVISOR_TOKEN", None)

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.bootstrap import bootstrap
from app.core import ha
from app.core.db import Base, SessionLocal, engine, local_today, utcnow
from app.main import app
from app.models import Household, Offer, Store, User


class FakeHomeAssistant:
    """Answers as Home Assistant does through the Supervisor: its configuration, its users, its
    entities, and the services MadPlan calls."""

    def __init__(self) -> None:
        self.language = "da"
        self.users: list[dict] = [{"id": "ha-admin", "is_owner": True, "group_ids": ["system-admin"]}]
        self.states: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.notify_services: list[str] = ["mobile_app_mor", "mobile_app_telefon"]
        # What ai_task.generate_data answers, in order. A dict is sent as structured data.
        self.ai_answers: list = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/core/api/config" or path == "/api/config":
            return httpx.Response(200, json={"language": self.language, "location_name": "Home", "version": "2026.11.0"})
        if path.endswith("/api/states"):
            return httpx.Response(200, json=self.states)
        if path.endswith("/api/services/ai_task/generate_data"):
            self.calls.append(("ai_task.generate_data", body))
            if not self.ai_answers:
                return httpx.Response(500, text="no answer scripted")
            answer = self.ai_answers.pop(0)
            if isinstance(answer, Exception):
                return httpx.Response(500, text=str(answer))
            data = answer if isinstance(answer, str) else json.dumps(answer)
            return httpx.Response(200, json={"changed_states": [], "service_response": {"conversation_id": "c", "data": data}})
        if path.endswith("/api/services") and request.method == "GET":
            notify = {name: {"name": name.replace("_", " ")} for name in self.notify_services}
            return httpx.Response(200, json=[{"domain": "notify", "services": {**notify, "send_message": {}}}, {"domain": "light", "services": {"turn_on": {}}}])
        if "/api/services/notify/" in path:
            self.calls.append((path.rsplit("/", 1)[-1], body))
            return httpx.Response(200, json=[])
        if path.endswith("/api/"):
            return httpx.Response(200, json={"message": "API running."})
        return httpx.Response(404, json={})


@pytest.fixture(autouse=True)
def home_assistant():
    fake = FakeHomeAssistant()
    ha.transport = httpx.MockTransport(fake.handler)
    ha.list_users = lambda: fake.users
    ha.set_options_for_tests(ha.AppOptions())
    ha.forget_cached_answers()
    yield fake
    ha.transport = None
    ha.list_users = None
    ha.set_options_for_tests(None)
    ha.forget_cached_answers()


def set_options(**values) -> ha.AppOptions:
    """The app's configuration in Home Assistant, as an administrator would set it."""
    options = ha.options().model_copy(update=values)
    ha.set_options_for_tests(options)
    return options


@pytest.fixture(autouse=True)
def fresh_database(home_assistant):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        bootstrap(session)
        # Planning, prices and the pantry are tested on MadPlan's own dishes, which are always
        # there. A new installation has them switched off, and test_new_recipes.py covers that.
        household = session.scalar(select(Household).order_by(Household.id).limit(1))
        household.settings = {**(household.settings or {}), "builtin_dishes": True}
        session.add(User(household_id=household.id, ha_user_id="ha-admin", username="admin", display_name="Admin", role="admin", locale="da", settings={}))
        session.commit()
    yield


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


def ingress_headers(ha_user_id: str, username: str, display_name: str | None = None) -> dict:
    return {"X-Remote-User-Id": ha_user_id, "X-Remote-User-Name": username, "X-Remote-User-Display-Name": display_name or username.title()}


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_client(client):
    """The Home Assistant owner, through ingress."""
    client.headers.update(ingress_headers("ha-admin", "admin", "Admin"))
    return client


def make_user(session, username="bruger", role="user", household_id=None, settings=None) -> User:
    """Someone with a Home Assistant user. An admin here is an administrator in Home Assistant."""
    if household_id is None:
        household_id = session.scalar(select(Household.id).order_by(Household.id).limit(1))
    user = User(household_id=household_id, ha_user_id=f"ha-{username}", username=username, display_name=username.title(), role=role, settings=settings or {})
    session.add(user)
    session.commit()
    users = ha.list_users()
    users.append({"id": f"ha-{username}", "is_owner": False, "group_ids": ["system-admin" if role == "admin" else "system-users"]})
    ha.forget_cached_answers()
    return user


def login_as(username: str) -> TestClient:
    """Opens the app as that person, from Home Assistant."""
    other = TestClient(app)
    other.headers.update(ingress_headers(f"ha-{username}", username))
    return other


_offer_counter = {"n": 0}


def add_offer(session, store_slug: str, title: str, price: float, **kwargs) -> Offer:
    _offer_counter["n"] += 1
    store = session.scalar(select(Store).where(Store.slug == store_slug))
    assert store is not None, f"unknown store {store_slug}"
    today = local_today()
    defaults = {
        "source": "test",
        "external_id": f"t{_offer_counter['n']}",
        "store_id": store.id,
        "title": title,
        "search_text": title.lower(),
        "price": price,
        "kind": "offer",
        "valid_from": today - timedelta(days=1),
        "valid_to": today + timedelta(days=14),
        "first_seen_at": utcnow(),
        "last_seen_at": utcnow(),
    }
    defaults.update(kwargs)
    offer = Offer(**defaults)
    session.add(offer)
    session.commit()
    return offer


def household_of_admin(session) -> Household:
    return session.scalar(select(Household).order_by(Household.id).limit(1))
