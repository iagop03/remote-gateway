from typing import Any

from pydantic import BaseModel


class EventOut(BaseModel):
    event_id: str
    session_id: str
    timestamp: str
    type: str
    data: dict[str, Any]


class EventHistoryResponse(BaseModel):
    events: list[EventOut]
    next_cursor: str | None = None
    total: int
