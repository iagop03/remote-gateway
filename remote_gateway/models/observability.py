from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DriverStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    available: bool
    status: str
    version: str | None = None
    reason: str | None = None


class HealthResponse(BaseModel):
    ok: bool
    version: str
    drivers: dict[str, DriverStatus]


class DriversResponse(BaseModel):
    available: list[str]
    unavailable: list[str]


class ModelInfo(BaseModel):
    id: str
    provider: str
    name: str
    capabilities: list[str] = Field(default_factory=list)


class ModelsResponse(BaseModel):
    models: list[ModelInfo]


class MetricsResponse(BaseModel):
    uptime_seconds: float
    # Shape differs by driver kind (session-oriented vs stateless HTTP) — see
    # Storage.session_stats() and HTTPDriver.metrics() — so kept loose here
    # rather than forcing a false union type.
    drivers: dict[str, dict[str, Any]]


class AuditEntryOut(BaseModel):
    id: int
    timestamp: str
    client_id: str
    driver: str
    model: str
    session_id: str | None = None
    input_tokens: int
    output_tokens: int
    origin_ip: str | None = None
    status: str
    working_directory: str | None = None


class LogsResponse(BaseModel):
    entries: list[AuditEntryOut]
    total: int


class UsageByDriver(BaseModel):
    driver: str
    input_tokens: int
    output_tokens: int
    calls: int


class UsageResponse(BaseModel):
    total_input_tokens: int
    total_output_tokens: int
    total_calls: int
    by_driver: list[UsageByDriver]
