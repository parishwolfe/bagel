from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Request

from api.runtime import BackgroundWorkers
from api.settings import Settings
from market.research.artifacts import Artifacts
from market.research.queue import Queue


@dataclass(frozen=True)
class Resources:
    settings: Settings
    queue: Queue
    artifacts: Artifacts
    workers: BackgroundWorkers


def get_resources(request: Request) -> Resources:
    return cast(Resources, request.app.state.resources)


ResourcesDep = Annotated[Resources, Depends(get_resources)]


def get_settings(resources: ResourcesDep) -> Settings:
    return resources.settings


SettingsDep = Annotated[Settings, Depends(get_settings)]
