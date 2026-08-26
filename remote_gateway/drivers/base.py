import uuid
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Awaitable, Callable

from ..storage import utc_now

EventPublisher = Callable[[dict[str, Any]], Awaitable[None]]


def make_event(session_id: str, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"event_id": f"evt_{uuid.uuid4().hex}", "session_id": session_id, "timestamp": utc_now(),
            "type": event_type, "data": data}


class LocalLLMDriver(ABC):
    name: str

    @abstractmethod
    async def detect(self) -> dict[str, Any]: ...

    @abstractmethod
    async def models(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def messages(self, body: dict[str, Any]) -> dict[str, Any] | AsyncIterator[dict[str, Any]]: ...

    async def run_session(
        self, session: dict[str, Any], body: dict[str, Any], publish: EventPublisher
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run one turn inside a persistent session, emitting real-time events via publish().

        Returns (message_result, session_updates) where session_updates are fields to
        persist back onto the session row (e.g. driver-specific resume state, status).
        Drivers that are inherently stateless (plain HTTP pass-through) can rely on this
        default, which just performs a single-shot call with no events.
        """
        return await self.messages(body), {}

    async def interrupt(self, session_id: str | None = None) -> None:
        return None

    def metrics(self) -> dict[str, Any]:
        """In-memory operational stats for GET /metrics (e.g. requests_total,
        avg_latency_ms for a stateless HTTP driver). Session-oriented drivers
        (SubprocessAgentDriver) are reported from storage instead — see
        Storage.session_stats() — since "how many sessions are active" is a
        durable fact, not something to track a second time in memory."""
        return {}
