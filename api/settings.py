from typing import Literal

from pydantic import Field, SecretStr, field_validator

from market.research.config import ResearchSettings


class Settings(ResearchSettings):
    """API-wide configuration, including the shared local database settings."""

    api_key: SecretStr | None = Field(
        default=None, validation_alias="BAGEL_API_KEY", repr=False, validate_default=True
    )
    ingestion_enabled: bool = Field(default=False, validation_alias="BAGEL_INGESTION_ENABLED")
    external_workers: bool = Field(default=False, validation_alias="BAGEL_EXTERNAL_WORKERS")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", validation_alias="BAGEL_LOG_LEVEL"
    )

    @field_validator("api_key")
    @classmethod
    def require_api_key(cls, value: SecretStr | None) -> SecretStr:
        if value is None or not value.get_secret_value().strip():
            raise ValueError("Set a nonempty BAGEL_API_KEY")
        return value
