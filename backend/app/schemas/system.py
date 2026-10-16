from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["healthy"] = "healthy"
    service: str
    version: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "unavailable"]
    checks: dict[str, Literal["up", "down"]]


class SystemInfo(BaseModel):
    name: str
    version: str
    api_version: str = "v1"
    environment: str
    phase: int = 12
