import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from unittest.mock import AsyncMock, patch

from remote_gateway.config import Settings
from remote_gateway.drivers.http import HTTPDriver
from remote_gateway.main import create_app


def client(tmp_path):
    return TestClient(create_app(Settings(database_path=str(tmp_path / "test.db"), token="secret", ollama_enabled=False)))


def test_health_requires_token(tmp_path):
    with client(tmp_path) as api:
        assert api.get("/health").status_code == 401
        assert api.get("/health", headers={"Authorization": "Bearer secret"}).json()["ok"] is True


def test_session_lifecycle_and_event_history(tmp_path):
    with client(tmp_path) as api:
        headers = {"Authorization": "Bearer secret"}
        response = api.post("/sessions", headers=headers, json={"driver": "claude-code", "model": "opus"})
        assert response.status_code == 200
        session_id = response.json()["session_id"]
        assert api.get(f"/sessions/{session_id}", headers=headers).json()["status"] == "idle"
        assert api.post(f"/sessions/{session_id}/interrupt", headers=headers).json() == {"ok": True}
        events = api.get(f"/sessions/{session_id}/events", headers=headers).json()["events"]
        assert events[0]["type"] == "session_interrupted"


def test_websocket_delivers_live_events_and_rejects_bad_token(tmp_path):
    with client(tmp_path) as api:
        headers = {"Authorization": "Bearer secret"}
        session_id = api.post("/sessions", headers=headers, json={"driver": "claude-code", "model": "opus"}).json()["session_id"]

        with api.websocket_connect(f"/sessions/{session_id}/events", headers=headers) as ws:
            api.post(f"/sessions/{session_id}/interrupt", headers=headers)
            event = ws.receive_json()
        assert event["type"] == "session_interrupted"

        with pytest.raises(WebSocketDisconnect):
            with api.websocket_connect(f"/sessions/{session_id}/events", headers={"Authorization": "Bearer wrong"}) as ws:
                ws.receive_json()


def test_can_interrupt_reflects_driver_capability_not_a_hardcoded_name(tmp_path):
    app = create_app(Settings(
        database_path=str(tmp_path / "test.db"), token="secret",
        ollama_enabled=True, gemini_enabled=True, codex_enabled=True,
    ))
    with TestClient(app) as api:
        headers = {"Authorization": "Bearer secret"}
        for driver_name in ("claude-code", "gemini", "codex"):
            body = api.post("/sessions", headers=headers, json={"driver": driver_name, "model": "x"}).json()
            assert body["can_interrupt"] is True, driver_name
        body = api.post("/sessions", headers=headers, json={"driver": "ollama", "model": "x"}).json()
        assert body["can_interrupt"] is False


def test_second_session_same_driver_conflicts_unless_allowed(tmp_path):
    app = create_app(Settings(database_path=str(tmp_path / "test.db"), token="secret"))
    with TestClient(app) as api:
        headers = {"Authorization": "Bearer secret"}
        first = api.post("/sessions", headers=headers, json={"driver": "claude-code", "model": "x"})
        assert first.status_code == 200
        second = api.post("/sessions", headers=headers, json={"driver": "claude-code", "model": "x"})
        assert second.status_code == 409

    allowed_app = create_app(Settings(
        database_path=str(tmp_path / "test2.db"), token="secret", allow_multiple_sessions_same_driver=True,
    ))
    with TestClient(allowed_app) as api:
        headers = {"Authorization": "Bearer secret"}
        assert api.post("/sessions", headers=headers, json={"driver": "claude-code", "model": "x"}).status_code == 200
        assert api.post("/sessions", headers=headers, json={"driver": "claude-code", "model": "x"}).status_code == 200


def test_max_concurrent_sessions_returns_429(tmp_path):
    app = create_app(Settings(
        database_path=str(tmp_path / "test.db"), token="secret",
        max_concurrent_sessions=1, allow_multiple_sessions_same_driver=True, gemini_enabled=True,
    ))
    with TestClient(app) as api:
        headers = {"Authorization": "Bearer secret"}
        assert api.post("/sessions", headers=headers, json={"driver": "claude-code", "model": "x"}).status_code == 200
        # Different driver too — the limit is global, not per-driver.
        response = api.post("/sessions", headers=headers, json={"driver": "gemini", "model": "x"})
        assert response.status_code == 429


def test_expired_session_rejects_new_messages(tmp_path):
    app = create_app(Settings(database_path=str(tmp_path / "test.db"), token="secret"))
    with TestClient(app) as api:
        headers = {"Authorization": "Bearer secret"}
        session_id = api.post("/sessions", headers=headers, json={"driver": "claude-code", "model": "x"}).json()["session_id"]
        app.state.storage.update_session(session_id, status="expired")

        response = api.post(f"/sessions/{session_id}/messages", headers=headers,
                             json={"messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 410


def test_metrics_reports_session_stats_and_http_driver_stats(tmp_path):
    response = type("Response", (), {"is_success": True, "raise_for_status": lambda self: None,
                                     "json": lambda self: {"message": {"content": "hello"}}})()
    fake_client = type("Client", (), {"__aenter__": AsyncMock(return_value=type("Client", (), {
        "post": AsyncMock(return_value=response)})()), "__aexit__": AsyncMock(return_value=None)})

    app = create_app(Settings(database_path=str(tmp_path / "test.db"), token="secret", ollama_enabled=True))
    with patch("remote_gateway.drivers.http.httpx.AsyncClient", return_value=fake_client()):
        with TestClient(app) as api:
            headers = {"Authorization": "Bearer secret"}
            api.post("/sessions", headers=headers, json={"driver": "claude-code", "model": "x"})
            api.post("/v1/messages", headers=headers, json={"model": "ollama:mistral", "messages": [{"role": "user", "content": "hi"}]})

            body = api.get("/metrics", headers=headers).json()
            assert body["uptime_seconds"] >= 0
            assert body["drivers"]["claude-code"] == {"sessions_active": 1, "messages_total": 0}
            assert body["drivers"]["ollama"]["requests_total"] == 1
            assert body["drivers"]["ollama"]["avg_latency_ms"] >= 0


def test_create_app_refuses_non_local_host_without_token(tmp_path):
    with pytest.raises(RuntimeError, match="Refusing to start"):
        create_app(Settings(database_path=str(tmp_path / "test.db"), host="0.0.0.0", token=""))


def test_create_app_allows_non_local_host_with_token(tmp_path):
    app = create_app(Settings(database_path=str(tmp_path / "test.db"), host="0.0.0.0", token="secret"))
    assert app is not None


def test_v1_messages_streams_real_incremental_deltas(tmp_path):
    ndjson_lines = [
        json.dumps({"message": {"content": "Hel"}, "done": False}),
        json.dumps({"message": {"content": "lo"}, "done": False}),
        json.dumps({"done": True, "prompt_eval_count": 3, "eval_count": 2}),
    ]

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        async def aiter_lines(self):
            for line in ndjson_lines:
                yield line

    class _FakeStreamCM:
        async def __aenter__(self):
            return _FakeResponse()

        async def __aexit__(self, *exc):
            return False

    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, **kwargs):
            return _FakeStreamCM()

    app = create_app(Settings(database_path=str(tmp_path / "test.db"), token="secret"))
    with patch("remote_gateway.drivers.http.httpx.AsyncClient", return_value=_FakeAsyncClient()):
        with TestClient(app) as api:
            headers = {"Authorization": "Bearer secret"}
            with api.stream("POST", "/v1/messages", headers=headers, json={
                "model": "ollama:mistral", "messages": [{"role": "user", "content": "hi"}], "stream": True,
            }) as response:
                raw_lines = [line for line in response.iter_lines() if line.startswith("data:")]

    deltas = [json.loads(line[len("data:"):]) for line in raw_lines if '"content_block_delta"' in line]
    assert [d["delta"]["text"] for d in deltas] == ["Hel", "lo"]


def test_audit_log_records_client_id_and_defaults_to_anonymous(tmp_path):
    response = type("Response", (), {"is_success": True, "raise_for_status": lambda self: None,
                                     "json": lambda self: {"message": {"content": "hello"}}})()
    fake_client = type("Client", (), {"__aenter__": AsyncMock(return_value=type("Client", (), {
        "post": AsyncMock(return_value=response)})()), "__aexit__": AsyncMock(return_value=None)})

    app = create_app(Settings(database_path=str(tmp_path / "test.db"), token="secret", ollama_enabled=True))
    with patch("remote_gateway.drivers.http.httpx.AsyncClient", return_value=fake_client()):
        with TestClient(app) as api:
            headers = {"Authorization": "Bearer secret"}
            body = {"model": "ollama:mistral", "messages": [{"role": "user", "content": "hi"}]}
            api.post("/v1/messages", headers={**headers, "X-Client-Id": "acme"}, json=body)
            api.post("/v1/messages", headers=headers, json=body)

            assert api.get("/logs").status_code == 401  # requires auth like everything else

            all_entries = api.get("/logs", headers=headers).json()["entries"]
            assert len(all_entries) == 2
            assert {e["client_id"] for e in all_entries} == {"acme", "anonymous"}

            filtered = api.get("/logs", headers=headers, params={"client_id": "acme"}).json()["entries"]
            assert len(filtered) == 1
            assert filtered[0]["driver"] == "ollama"
            assert filtered[0]["origin_ip"]  # TestClient sets a client host


def test_ollama_response_is_normalized():
    response = type("Response", (), {"is_success": True, "raise_for_status": lambda self: None,
                                     "json": lambda self: {"message": {"content": "hello"}}})()
    client = type("Client", (), {"__aenter__": AsyncMock(return_value=type("Client", (), {
        "post": AsyncMock(return_value=response)})()), "__aexit__": AsyncMock(return_value=None)})
    with patch("remote_gateway.drivers.http.httpx.AsyncClient", return_value=client()):
        result = __import__("asyncio").run(HTTPDriver("ollama", "http://localhost").messages({"model": "ollama:mistral", "messages": []}))
    assert result["content"][0]["text"] == "hello"
