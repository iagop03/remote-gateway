import asyncio
import functools
import hmac
import json
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from .config import LOCAL_HOSTS, Settings, get_settings
from .drivers import (
    ClaudeCodeDriver, CodexDriver, GeminiDriver, HTTPDriver,
    LocalLLMDriver, SubprocessAgentDriver, make_event,
)
from .logging import configure_logging, get_logger
from .models import (
    CreateSessionRequest, DriversResponse, EventHistoryResponse, HealthResponse,
    LogsResponse, MessageResponse, MessagesRequest, MetricsResponse,
    ModelsResponse, SessionCreatedResponse, SessionDetailResponse,
)
from .storage import Storage, utc_now

_SESSION_SWEEP_INTERVAL_SECONDS = 60
log = get_logger(__name__)


def _package_version() -> str:
    try:
        return _pkg_version("remote-gateway")
    except PackageNotFoundError:
        return "0.0.0-dev"


class UTF8JSONResponse(JSONResponse):
    # Windows PowerShell 5.1's Invoke-RestMethod assumes ISO-8859-1 when the
    # Content-Type header omits an explicit charset, mangling non-ASCII text.
    media_type = "application/json; charset=utf-8"


def build_drivers(settings: Settings) -> dict[str, LocalLLMDriver]:
    drivers: dict[str, LocalLLMDriver] = {}
    if settings.claude_code_enabled:
        drivers["claude-code"] = ClaudeCodeDriver(
            timeout_seconds=settings.claude_code_timeout_seconds,
            allowed_working_directories=settings.allowed_working_directories_list(),
        )
    if settings.gemini_enabled:
        drivers["gemini"] = GeminiDriver(
            timeout_seconds=settings.gemini_timeout_seconds,
            allowed_working_directories=settings.allowed_working_directories_list(),
        )
    if settings.codex_enabled:
        drivers["codex"] = CodexDriver(
            timeout_seconds=settings.codex_timeout_seconds,
            allowed_working_directories=settings.allowed_working_directories_list(),
        )
    if settings.ollama_enabled:
        drivers["ollama"] = HTTPDriver("ollama", settings.ollama_base_url)
    if settings.lmstudio_enabled:
        drivers["lmstudio"] = HTTPDriver("lmstudio", settings.lmstudio_base_url)
    if settings.vllm_enabled:
        drivers["vllm"] = HTTPDriver("vllm", settings.vllm_base_url)
    if settings.localai_enabled:
        drivers["localai"] = HTTPDriver("localai", settings.localai_base_url)
    return drivers


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    if settings.host not in LOCAL_HOSTS and not settings.token:
        raise RuntimeError(
            f"Refusing to start: REMOTE_GATEWAY_HOST={settings.host!r} is not localhost-only and "
            "REMOTE_GATEWAY_TOKEN is empty. A gateway that can run shell commands via Claude Code "
            "must not be reachable from the network without auth. Set REMOTE_GATEWAY_TOKEN first."
        )
    storage = Storage(settings.database_path)
    drivers = build_drivers(settings)
    subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
    start_time = time.monotonic()

    async def _db(func, *args: Any, **kwargs: Any) -> Any:
        # Storage is plain sqlite3 (synchronous); offload it so it never blocks the
        # event loop, which matters once a session is publishing many small events
        # per turn (one per streamed token).
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))

    async def authenticate(authorization: str | None = Header(default=None)) -> None:
        if not settings.token:
            return
        supplied = authorization.removeprefix("Bearer ") if authorization else ""
        if not hmac.compare_digest(supplied, settings.token):
            raise HTTPException(status_code=401, detail="Invalid bearer token")

    async def publish(event: dict[str, Any]) -> None:
        await _db(storage.add_event, event)
        for queue in subscribers.get(event["session_id"], set()):
            await queue.put(event)

    def _origin_ip(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else ""

    async def _audit(
        request: Request, driver_name: str, session_id: str | None, body: dict[str, Any], message: dict[str, Any]
    ) -> None:
        # Records who consumed what — separate from the session event log, which is
        # about replaying a conversation, not billing/usage attribution. A caller
        # self-identifies with X-Client-Id; without one, entries are "anonymous"
        # rather than silently dropped, so nothing goes unaccounted for.
        usage = message.get("usage") or {}
        await _db(storage.add_audit_entry, {
            "timestamp": utc_now(), "client_id": request.headers.get("x-client-id", "anonymous"),
            "driver": driver_name, "model": body.get("model", ""), "session_id": session_id,
            "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
            "origin_ip": _origin_ip(request),
            "status": "error" if message.get("stop_reason") == "error" else "ok",
        })

    async def _session_reaper() -> None:
        while True:
            await asyncio.sleep(_SESSION_SWEEP_INTERVAL_SECONDS)
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=settings.session_timeout_minutes)).isoformat()
            try:
                expired = await _db(storage.expire_stale_sessions, cutoff)
                if expired:
                    log.info("sessions expired due to inactivity", count=expired,
                              timeout_minutes=settings.session_timeout_minutes)
            except Exception:
                log.exception("session reaper sweep failed")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        log.info("remote-gateway starting", drivers=sorted(drivers), host=settings.host, port=settings.port,
                  auth_required=bool(settings.token))
        reaper_task = asyncio.create_task(_session_reaper(), name="session-reaper")
        yield
        reaper_task.cancel()
        with suppress(asyncio.CancelledError):
            await reaper_task
        storage.connection.close()
        log.info("remote-gateway stopped")

    app = FastAPI(title="Remote Gateway", version=_package_version(), lifespan=lifespan, default_response_class=UTF8JSONResponse)
    app.state.storage = storage  # exposed for tests; not used by request handlers, which close over `storage` directly

    @app.get("/health", dependencies=[Depends(authenticate)], response_model=HealthResponse)
    async def health() -> dict[str, Any]:
        statuses = {name: await driver.detect() for name, driver in drivers.items()}
        return {"ok": True, "version": app.version, "drivers": statuses}

    @app.get("/drivers", dependencies=[Depends(authenticate)], response_model=DriversResponse)
    async def driver_list() -> dict[str, list[str]]:
        statuses = {name: await driver.detect() for name, driver in drivers.items()}
        return {"available": [n for n, s in statuses.items() if s["available"]], "unavailable": [n for n, s in statuses.items() if not s["available"]]}

    @app.get("/models", dependencies=[Depends(authenticate)], response_model=ModelsResponse)
    async def model_list() -> dict[str, list[dict[str, Any]]]:
        result: list[dict[str, Any]] = []
        for driver in drivers.values():
            result.extend(await driver.models())
        return {"models": result}

    @app.get("/metrics", dependencies=[Depends(authenticate)], response_model=MetricsResponse)
    async def metrics() -> dict[str, Any]:
        # Session-oriented drivers (Claude Code, Gemini, Codex) report from
        # storage — sessions_active/messages_total are durable facts, not something
        # to track a second time in memory. Stateless HTTP drivers (Ollama, LM
        # Studio, vLLM, LocalAI) self-report requests_total/avg_latency_ms instead,
        # since they have no session rows to query.
        driver_stats: dict[str, Any] = {}
        for name, driver in drivers.items():
            if isinstance(driver, SubprocessAgentDriver):
                driver_stats[name] = await _db(storage.session_stats, name)
            else:
                driver_stats[name] = driver.metrics()
        return {"uptime_seconds": round(time.monotonic() - start_time, 1), "drivers": driver_stats}

    @app.get("/logs", dependencies=[Depends(authenticate)], response_model=LogsResponse)
    async def logs(
        limit: int = Query(100, ge=1, le=1000), client_id: str | None = None, since: str | None = None,
    ) -> dict[str, Any]:
        entries = await _db(storage.audit_entries, limit, client_id, since)
        return {"entries": entries, "total": len(entries)}

    async def _stream_message_events(
        driver: LocalLLMDriver, driver_name: str, body: dict[str, Any], request: Request
    ) -> AsyncIterator[bytes]:
        # Reuses the same run_session()/publish event flow that sessions use, so a
        # stateless /v1/messages call streams real incremental deltas as the driver
        # produces them instead of buffering the whole reply and faking one SSE chunk.
        # This call is deliberately not registered as a session: nothing is persisted
        # to storage or broadcast to /sessions WebSocket subscribers.
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        session = {"session_id": f"sess_{uuid.uuid4().hex}", "working_directory": body.get("working_directory")}
        final_message: dict[str, Any] = {}

        async def local_publish(event: dict[str, Any]) -> None:
            await queue.put(event)

        async def runner() -> None:
            nonlocal final_message
            try:
                final_message, _ = await driver.run_session(session, body, local_publish)
            except RuntimeError as exc:
                await queue.put(make_event(session["session_id"], "error", {"message": str(exc)}))
            finally:
                await queue.put(None)

        task = asyncio.create_task(runner())
        yield f"data: {json.dumps({'type': 'content_block_start', 'content_block': {'type': 'text', 'text': ''}})}\n\n".encode()
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                if event["type"] == "assistant_message":
                    text = event["data"].get("text", "")
                    if text:
                        yield f"data: {json.dumps({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': text}})}\n\n".encode()
                elif event["type"] == "error":
                    yield f"data: {json.dumps({'type': 'error', 'error': event['data']})}\n\n".encode()
        finally:
            await task
        if final_message:
            await _audit(request, driver_name, session["session_id"], body, final_message)
        yield b"data: {\"type\": \"content_block_stop\"}\n\n"
        yield b"data: {\"type\": \"message_stop\"}\n\n"

    @app.post("/v1/messages", dependencies=[Depends(authenticate)], response_model=MessageResponse)
    async def messages(payload: MessagesRequest, request: Request) -> Any:
        # Validated at the boundary (model/messages/stream/working_directory),
        # then back to a plain dict — driver internals still work with
        # dict.get(...) throughout, and extra=allow fields (temperature, tools,
        # permission_mode, ...) survive the round-trip for HTTPDriver pass-through.
        body = payload.model_dump()
        driver_name = body.get("model", "").split(":", 1)[0]
        driver = drivers.get(driver_name)
        if not driver:
            raise HTTPException(status_code=400, detail="Model must use a known provider prefix")
        if body.get("stream"):
            return StreamingResponse(_stream_message_events(driver, driver_name, body, request), media_type="text/event-stream")
        try:
            result = await driver.messages(body)
        except RuntimeError as exc:
            log.warning("turn failed", driver=driver_name, error=str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        await _audit(request, driver_name, None, body, result)
        return result

    @app.post("/sessions", dependencies=[Depends(authenticate)], response_model=SessionCreatedResponse)
    async def create_session(payload: CreateSessionRequest) -> dict[str, Any]:
        body = payload.model_dump()
        driver_name = body.get("driver", "")
        if driver_name not in drivers:
            raise HTTPException(status_code=400, detail="Unknown driver")
        if not settings.allow_multiple_sessions_same_driver and await _db(storage.count_active_sessions, driver_name):
            raise HTTPException(
                status_code=409,
                detail=f"An active session already exists for driver '{driver_name}' "
                       "(set ALLOW_MULTIPLE_SESSIONS_SAME_DRIVER=true to allow more than one)",
            )
        total_active = await _db(storage.count_active_sessions, None)
        if total_active >= settings.max_concurrent_sessions:
            raise HTTPException(
                status_code=429,
                detail=f"MAX_CONCURRENT_SESSIONS={settings.max_concurrent_sessions} reached "
                       f"({total_active} active) — stop or let an existing session finish first",
            )
        now = utc_now()
        session = {"id": f"sess_{uuid.uuid4().hex}", "driver": driver_name, "model": body.get("model", ""),
                   "working_directory": body.get("working_directory"), "status": "idle", "created_at": now, "last_activity": now}
        await _db(storage.create_session, session)
        log.info("session created", session_id=session["id"], driver=driver_name)
        return {"session_id": session["id"], **{key: session[key] for key in ("driver", "status", "created_at")},
                "can_interrupt": isinstance(drivers[driver_name], SubprocessAgentDriver), "can_reconnect": True}

    @app.get("/sessions/{session_id}", dependencies=[Depends(authenticate)], response_model=SessionDetailResponse)
    async def get_session(session_id: str) -> dict[str, Any]:
        session = await _db(storage.get_session, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        session["session_id"] = session.pop("id")
        return session

    @app.get("/sessions/{session_id}/events", dependencies=[Depends(authenticate)], response_model=EventHistoryResponse)
    async def event_history(session_id: str, limit: int = Query(50, ge=1, le=500), after: str | None = None) -> dict[str, Any]:
        if not await _db(storage.get_session, session_id):
            raise HTTPException(status_code=404, detail="Session not found")
        events = await _db(storage.events, session_id, limit, after)
        return {"events": events, "next_cursor": events[-1]["event_id"] if events else None, "total": len(events)}

    @app.post("/sessions/{session_id}/messages", dependencies=[Depends(authenticate)], response_model=MessageResponse)
    async def send_message(session_id: str, payload: MessagesRequest, request: Request) -> Any:
        session = await _db(storage.get_session, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if session["status"] == "expired":
            raise HTTPException(
                status_code=410,
                detail=f"Session expired after {settings.session_timeout_minutes}min of inactivity — create a new one",
            )
        driver = drivers.get(session["driver"])
        if not driver:
            raise HTTPException(status_code=400, detail="Unknown driver")
        body = payload.model_dump()
        # payload.model always has a key (default "") even when the caller omits
        # it, so a plain `body.get("model", session["model"])` would never fall
        # back — fix the value itself before it reaches the driver.
        body["model"] = body.get("model") or session["model"]
        await _db(storage.update_session, session_id, status="processing", last_activity=utc_now())
        try:
            result, updates = await driver.run_session(session, body, publish)
        except RuntimeError as exc:
            await _db(storage.update_session, session_id, status="error", last_activity=utc_now())
            log.warning("turn failed", session_id=session_id, driver=session["driver"], error=str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        await _db(
            storage.update_session, session_id, last_activity=utc_now(), message_count=session["message_count"] + 1,
            status=updates.get("status", "idle"),
            **{key: value for key, value in updates.items() if key != "status"},
        )
        await _audit(request, session["driver"], session_id, body, result)
        return result

    @app.post("/sessions/{session_id}/interrupt", dependencies=[Depends(authenticate)])
    async def interrupt_session(session_id: str) -> dict[str, bool]:
        session = await _db(storage.get_session, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        await drivers[session["driver"]].interrupt(session_id)
        await _db(storage.update_session, session_id, status="interrupted", last_activity=utc_now())
        await publish(make_event(session_id, "session_interrupted", {}))
        return {"ok": True}

    @app.post("/sessions/{session_id}/stop", dependencies=[Depends(authenticate)])
    async def stop_session(session_id: str) -> dict[str, bool]:
        session = await _db(storage.get_session, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        await drivers[session["driver"]].interrupt(session_id)
        await _db(storage.update_session, session_id, status="completed", last_activity=utc_now())
        await publish(make_event(session_id, "session_completed", {"reason": "stopped"}))
        return {"ok": True}

    @app.websocket("/sessions/{session_id}/events")
    async def events_socket(websocket: WebSocket, session_id: str, after: str | None = None):
        if settings.token and not hmac.compare_digest(websocket.headers.get("authorization", "").removeprefix("Bearer "), settings.token):
            await websocket.close(code=1008)
            return
        if not await _db(storage.get_session, session_id):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        subscribers.setdefault(session_id, set()).add(queue)
        try:
            for event in await _db(storage.events, session_id, 50, after):
                await websocket.send_json(event)
            while True:
                await websocket.send_json(await queue.get())
        except WebSocketDisconnect:
            pass
        finally:
            remaining = subscribers.get(session_id)
            if remaining is not None:
                remaining.discard(queue)
                if not remaining:
                    subscribers.pop(session_id, None)

    return app


app = create_app()
