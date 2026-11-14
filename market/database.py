"""Shared PostgreSQL configuration and SQLAlchemy-managed connection pooling."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Self

from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, create_engine
from sqlalchemy import inspect as inspect_model
from sqlalchemy.orm import Session

LOCAL_DATABASE_HOSTS = {"127.0.0.1", "localhost", "::1", "db"}


def require_local_database(dsn: str) -> None:
    params = conninfo_to_dict(dsn)
    if (
        params.get("host") not in LOCAL_DATABASE_HOSTS
        or "hostaddr" in params
        or "service" in params
        or os.environ.get("PGHOSTADDR")
        or os.environ.get("PGSERVICE")
    ):
        raise ValueError("Backend requires an explicit local database host")


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    database_url: SecretStr | None = Field(default=None, validation_alias="DATABASE_URL", repr=False)
    db_host: str | None = Field(default=None, validation_alias="DB_HOST", repr=False)
    db_port: int | None = Field(default=None, validation_alias="DB_PORT", ge=1, le=65535)
    db_database: str | None = Field(default=None, validation_alias="DB_DATABASE", repr=False)
    db_user: str | None = Field(default=None, validation_alias="DB_USER", repr=False)
    db_password: SecretStr | None = Field(default=None, validation_alias="DB_PASSWORD", repr=False)

    @model_validator(mode="after")
    def configure_database(self) -> Self:
        fields = {
            "DB_HOST": self.db_host,
            "DB_PORT": self.db_port,
            "DB_DATABASE": self.db_database,
            "DB_USER": self.db_user,
            "DB_PASSWORD": self.db_password,
        }
        if any(value is not None for value in fields.values()):
            if missing := [name for name, value in fields.items() if value is None]:
                raise ValueError("Missing database configuration: " + ", ".join(missing))
            assert self.db_password is not None
            self.database_url = SecretStr(
                make_conninfo(
                    host=self.db_host,
                    port=self.db_port,
                    dbname=self.db_database,
                    user=self.db_user,
                    password=self.db_password.get_secret_value(),
                    sslmode="disable" if self.db_host in LOCAL_DATABASE_HOSTS else "require",
                )
            )
        return self


def database_engine(dsn: str, *, application_name: str, pool_size: int, timeout: int) -> Engine:
    return create_engine(
        "postgresql+psycopg://",
        connect_args={
            **conninfo_to_dict(dsn),
            "application_name": application_name,
            "connect_timeout": timeout,
            "prepare_threshold": None,
        },
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=0,
        pool_timeout=timeout,
        hide_parameters=True,
    )


@contextmanager
def database_session(engine: Engine) -> Iterator[Session]:
    """One explicit ORM transaction; never share sessions across threads."""
    with Session(engine, expire_on_commit=False) as session, session.begin():
        yield session


def row_dict(instance: Any) -> dict:
    """Detached value view, so nested JSON edits cannot mutate an ORM cache silently."""
    return {
        column.name: deepcopy(
            getattr(instance, inspect_model(type(instance)).get_property_by_column(column).key)
        )
        for column in inspect_model(type(instance)).columns
    }
