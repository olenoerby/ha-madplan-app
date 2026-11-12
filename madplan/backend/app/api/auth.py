"""Who the person using the app is. Logging in and out is Home Assistant's business."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, Db
from app.core import ha
from app.models import User
from app.settings_schema import household_settings, user_settings

router = APIRouter(prefix="/auth", tags=["auth"])


def user_payload(db, user: User) -> dict:
    hs = household_settings(user.household.settings)
    options = ha.options()
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "locale": user.locale,
        "household": {
            "id": user.household_id,
            "name": user.household.name,
            "default_adults": hs.default_adults,
            "default_children": hs.default_children,
        },
        "settings": user_settings(user.settings).model_dump(mode="json"),
        # What the administrator has set up in the app's configuration in Home Assistant. The
        # pages show Mealie's and the AI's buttons only when they can work.
        "setup": {"mealie": options.mealie_configured, "ai": options.ai_configured},
    }


@router.get("/me")
def me(user: CurrentUser, db: Db) -> dict:
    return user_payload(db, user)
