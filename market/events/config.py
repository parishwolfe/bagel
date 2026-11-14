from pydantic import Field, field_validator

from market.research.config import ResearchSettings


class EventSettings(ResearchSettings):
    database_budget_mb: int = Field(default=100_000, ge=1, validation_alias="BAGEL_DATABASE_BUDGET_MB")
    redis_url: str = Field(default="redis://redis:6379/0", validation_alias="BAGEL_REDIS_URL")
    stream_max_length: int = Field(
        default=200_000, ge=1000, le=10_000_000, validation_alias="BAGEL_STREAM_MAX_LENGTH"
    )
    publication_batch: int = Field(default=1000, ge=1, le=10_000, validation_alias="BAGEL_PUBLICATION_BATCH")
    research_processes: int = Field(default=2, ge=1, le=12, validation_alias="BAGEL_RESEARCH_PROCESSES")
    research_pending_budget: int = Field(
        default=256, ge=1, le=100_000, validation_alias="BAGEL_RESEARCH_PENDING_BUDGET"
    )

    @field_validator("redis_url")
    @classmethod
    def local_redis(cls, value: str) -> str:
        if value not in {"redis://redis:6379/0", "redis://127.0.0.1:6379/0", "redis://localhost:6379/0"}:
            raise ValueError("Use an explicit local Redis endpoint without embedded credentials")
        return value
