from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from market.database import DatabaseSettings, require_local_database


class ResearchSettings(DatabaseSettings):
    artifacts: Path = Field(default=Path(".bagel/research"), validation_alias="BAGEL_ARTIFACTS")

    @model_validator(mode="after")
    def check_local(self) -> Self:
        if self.database_url is None:
            raise ValueError("Set DB_HOST, DB_PORT, DB_DATABASE, DB_USER and DB_PASSWORD")
        require_local_database(self.database_url.get_secret_value())
        return self

    def dsn(self) -> str:
        assert self.database_url is not None
        return self.database_url.get_secret_value()
