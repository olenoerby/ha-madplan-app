"""Who is asking.

People come in through Home Assistant: its login, its users. The Supervisor's ingress proxy
passes on who the user is in headers, and only requests from that proxy may name a user that
way. Home Assistant's administrators are MadPlan's administrators. A personal API token, for a
Home Assistant integration that reads MadPlan, is the only other way in.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import ha
from app.core.config import settings
from app.core.db import get_db, utcnow
from app.core.security import token_hash
from app.models import ApiToken, Household, User
from app.settings_schema import user_settings

Db = Annotated[Session, Depends(get_db)]

USER_ID_HEADER = "x-remote-user-id"
USER_NAME_HEADER = "x-remote-user-name"
DISPLAY_NAME_HEADER = "x-remote-user-display-name"
# What MadPlan's own roles can be, besides admin, which follows Home Assistant.
LOCAL_ROLES = ("household_manager", "user", "guest")


def _from_bearer(db: Session, request: Request) -> User | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    row = db.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash(header[7:].strip())))
    if row is None:
        return None
    row.last_used_at = utcnow()
    db.commit()
    return db.get(User, row.user_id)


def _through_ingress(request: Request) -> bool:
    if settings.trust_ingress_headers_from_anywhere:
        return True
    return bool(request.client and request.client.host == settings.ingress_proxy_ip)


def the_household(db: Session) -> Household:
    """MadPlan in Home Assistant is one home: everyone belongs to the same household."""
    household = db.scalar(select(Household).order_by(Household.id).limit(1))
    if household is None:
        household = Household(name=ha.ha_config().get("location_name") or "Home", settings={})
        db.add(household)
        db.flush()
    return household


def _unique_username(db: Session, wanted: str, ha_user_id: str) -> str:
    base = re.sub(r"[^a-z0-9._-]", "", wanted.lower())[:60] or f"user-{ha_user_id[:8]}"
    name, n = base, 1
    while db.scalar(select(func.count(User.id)).where(User.username == name)):
        n += 1
        name = f"{base}-{n}"
    return name


def user_from_ingress(db: Session, ha_user_id: str, username: str | None, display_name: str | None) -> User:
    """The MadPlan user for a Home Assistant user, made the first time they open the app."""
    user = db.scalar(select(User).where(User.ha_user_id == ha_user_id))
    if user is None:
        user = User(
            household_id=the_household(db).id,
            ha_user_id=ha_user_id,
            username=_unique_username(db, username or display_name or "", ha_user_id),
            display_name=(display_name or username or "Home Assistant")[:120],
            locale=ha.ha_language(),
            role="user",
            settings={},
        )
        db.add(user)
        db.flush()
    elif display_name and user.display_name != display_name[:120]:
        user.display_name = display_name[:120]
    # Danish when Home Assistant is in Danish, English otherwise.
    user.locale = ha.ha_language()
    # The admin role follows Home Assistant. When Home Assistant cannot be asked, it stays as it was.
    admins = ha.ha_admin_ids()
    if admins is not None:
        if ha_user_id in admins:
            user.role = "admin"
        elif user.role == "admin":
            user.role = "user"
    if user.last_login_at is None or (utcnow() - user.last_login_at).total_seconds() > 3600:
        user.last_login_at = utcnow()
    db.commit()
    return user


# What a guest may change: a vote, and things that are only their own.
_GUEST_MAY_WRITE = re.compile(
    r"^/api/v1/("
    r"(ha/)?meals/\d+/vote"
    r"|me(/settings|/delete|/tokens(/\d+)?|/items(/\d+)?|/stores/\d+)?"
    r"|notifications/(read-all|\d+/read)"
    r")$"
)


def current_user(request: Request, db: Db) -> User:
    user = _from_bearer(db, request)
    if user is None and _through_ingress(request):
        ha_user_id = request.headers.get(USER_ID_HEADER)
        if ha_user_id:
            user = user_from_ingress(db, ha_user_id, request.headers.get(USER_NAME_HEADER), request.headers.get(DISPLAY_NAME_HEADER))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
    # One check for every route, so a new route is closed to guests unless listed above.
    if user.is_guest and request.method not in ("GET", "HEAD", "OPTIONS") and not _GUEST_MAY_WRITE.match(request.url.path):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "guest_read_only")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def require_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin_required")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def require_manager(user: CurrentUser) -> User:
    if not user.manages_household:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "manager_required")
    return user


ManagerUser = Annotated[User, Depends(require_manager)]


def household_of(db: Session, user: User) -> Household:
    return db.get(Household, user.household_id)


def require_feature(user: User, feature: str) -> None:
    """Honours a user's own switch for a feature."""
    if not getattr(user_settings(user.settings).features, feature, True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"feature_disabled:{feature}")
