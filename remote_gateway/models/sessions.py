from pydantic import BaseModel, ConfigDict


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    driver: str
    model: str = ""
    working_directory: str | None = None


class SessionCreatedResponse(BaseModel):
    session_id: str
    driver: str
    status: str
    created_at: str
    can_interrupt: bool
    can_reconnect: bool


class SessionDetailResponse(BaseModel):
    session_id: str
    driver: str
    model: str
    working_directory: str | None = None
    status: str
    created_at: str
    last_activity: str
    message_count: int
    driver_session_id: str | None = None
