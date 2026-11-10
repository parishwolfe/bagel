from secrets import compare_digest
from typing import Annotated

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from api.dependencies import SettingsDep

api_key_header = APIKeyHeader(name="X-API-Key", scheme_name="APIKey", auto_error=False)


def require_api_key(settings: SettingsDep, key: Annotated[str | None, Security(api_key_header)]) -> None:
    assert settings.api_key is not None
    expected = settings.api_key.get_secret_value().encode("utf-8")
    if key is None or not compare_digest(key.encode("utf-8"), expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "APIKey"},
        )
