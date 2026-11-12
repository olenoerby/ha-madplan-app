"""Process configuration.

MadPlan runs as a Home Assistant app. What an administrator chooses (Mealie, the AI agent, the
schedule) is in the app's configuration in Home Assistant, which the Supervisor writes to
``/data/options.json``: see ``app/core/ha.py``. What is set here is how the process runs, and
can be changed with environment variables named ``MADPLAN_<NAME>``, as tests and local
development do.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MADPLAN_", extra="ignore")

    app_name: str = "MadPlan"
    app_env: str = "development"
    timezone: str = "Europe/Copenhagen"

    # Everything the app keeps is in /data, which Home Assistant includes in its backups.
    data_dir: str = "/data"
    database_url: str | None = None
    options_file: str | None = None

    # The Supervisor's proxy to Home Assistant, and the token it gives every app.
    ha_core_url: str = "http://supervisor/core"
    supervisor_token: str | None = None
    # Ingress requests come from the Supervisor. Only those may name the user in a header.
    ingress_proxy_ip: str = "172.30.32.2"
    trust_ingress_headers_from_anywhere: bool = False
    # Where the app writes a photo for Home Assistant's AI Task to read, as a media source.
    media_dir: str = "/media"

    default_adults: int = 2
    default_children: int = 0

    crawler_user_agent: str = "MadPlanBot/1.0 (+https://github.com/olenoerby/ha-madplan-app)"
    nominatim_url: str = "https://nominatim.openstreetmap.org"

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def sqlalchemy_url(self) -> str:
        return self.database_url or f"sqlite:///{Path(self.data_dir) / 'madplan.db'}"

    @property
    def options_path(self) -> Path:
        return Path(self.options_file or Path(self.data_dir) / "options.json")

    @property
    def token(self) -> str | None:
        """The Supervisor's token, which the Supervisor puts in the app's environment."""
        return self.supervisor_token or os.environ.get("SUPERVISOR_TOKEN")

    @property
    def effective_secret(self) -> bytes:
        """A key of the app's own, made once and kept in /data, for anything stored encrypted."""
        path = Path(self.data_dir) / "secret.key"
        try:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
                path.chmod(0o600)
            secret = path.read_text(encoding="utf-8").strip()
        except OSError:
            # Tests and local development without a writable /data.
            secret = "madplan-development-only-secret"  # noqa: S105 (only without a writable /data)
        return hashlib.sha256(secret.encode()).digest()


settings = Settings()
