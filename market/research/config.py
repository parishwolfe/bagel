import os
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit
from uuid import UUID

from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_DB_HOSTS = {"127.0.0.1", "localhost", "::1", "supabase_db_bagel"}
LOCAL_AUTH_HOSTS = {"127.0.0.1", "localhost", "::1", "supabase_kong_bagel"}


def require_local_database(dsn: str) -> None:
    params = conninfo_to_dict(dsn)
    if (
        params.get("host") not in LOCAL_DB_HOSTS
        or "hostaddr" in params
        or "service" in params
        or os.environ.get("PGHOSTADDR")
        or os.environ.get("PGSERVICE")
    ):
        raise ValueError("Research requires an explicit local database host")


class ResearchSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BAGEL_RESEARCH_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    database_url: SecretStr = SecretStr(
        "postgresql://bagel_research_worker:bagel-local-research@127.0.0.1:54322/postgres"
    )
    artifacts: Path = Path(".bagel/research")
    auth_url: str = "http://127.0.0.1:54321"
    auth_key: SecretStr | None = None
    operator_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def check_local(self) -> Self:
        require_local_database(self.database_url.get_secret_value())
        url = urlsplit(self.auth_url)
        if (
            url.scheme != "http"
            or url.hostname not in LOCAL_AUTH_HOSTS
            or url.path not in ("", "/")
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Research Auth URL must be a local Supabase origin")
        return self
