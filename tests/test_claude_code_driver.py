import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from remote_gateway.drivers.claude_code import ClaudeCodeDriver


class _HangingProcess:
    """Simulates a `claude` subprocess that never writes output and reacts to a signal."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdout = self
        self.stderr = self
        self.sent_signals: list[int] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)
        raise StopAsyncIteration  # pragma: no cover - never reached

    async def read(self) -> bytes:
        return b""

    def send_signal(self, sig: int) -> None:
        self.sent_signals.append(sig)
        self.returncode = -2  # process reacts to the signal and exits

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode


def test_translate_init_line_captures_claude_session_id():
    driver = ClaudeCodeDriver()
    line = {"type": "system", "subtype": "init", "session_id": "abc-123", "model": "claude-sonnet-5", "cwd": "C:\\proj"}
    events, claude_session_id, result = driver._translate(line, "sess_1", None)
    assert claude_session_id == "abc-123"
    assert result is None
    assert events[0]["type"] == "session_started"
    assert events[0]["data"]["claude_session_id"] == "abc-123"


def test_translate_text_delta_is_partial_assistant_message():
    driver = ClaudeCodeDriver()
    line = {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}}
    events, claude_session_id, result = driver._translate(line, "sess_1", "abc-123")
    assert claude_session_id == "abc-123"
    assert events == [{"event_id": events[0]["event_id"], "session_id": "sess_1", "timestamp": events[0]["timestamp"],
                        "type": "assistant_message", "data": {"text": "Hi", "partial": True}}]


def test_translate_tool_use_start_emits_tool_started():
    driver = ClaudeCodeDriver()
    line = {"type": "stream_event", "event": {"type": "content_block_start",
            "content_block": {"type": "tool_use", "id": "tu_1", "name": "Bash"}}}
    events, _, _ = driver._translate(line, "sess_1", None)
    assert events[0]["type"] == "tool_started"
    assert events[0]["data"] == {"id": "tu_1", "name": "Bash"}


def test_translate_result_line_emits_session_completed():
    driver = ClaudeCodeDriver()
    line = {"type": "result", "result": "Four", "is_error": False, "usage": {"input_tokens": 2, "output_tokens": 4}}
    events, _, result = driver._translate(line, "sess_1", "abc-123")
    assert result is line
    assert events[0]["type"] == "session_completed"
    assert events[0]["data"]["result"] == "Four"


def test_translate_error_result_also_emits_error_event():
    driver = ClaudeCodeDriver()
    line = {"type": "result", "result": "boom", "is_error": True, "usage": {}}
    events, _, _ = driver._translate(line, "sess_1", None)
    types = [event["type"] for event in events]
    assert types == ["session_completed", "error"]


def test_extract_prompt_uses_last_user_message():
    messages = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "reply"},
                {"role": "user", "content": [{"type": "text", "text": "second"}]}]
    assert ClaudeCodeDriver._extract_prompt(messages) == "second"


def test_build_command_adds_resume_flag_when_continuing():
    driver = ClaudeCodeDriver()
    cmd = driver._build_command({"messages": [{"role": "user", "content": "hi"}], "model": "claude-code:opus"}, "abc-123")
    assert "--resume" in cmd
    assert cmd[cmd.index("--resume") + 1] == "abc-123"
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "opus"


def test_resolve_cwd_allows_paths_inside_whitelist(tmp_path):
    allowed_root = tmp_path / "projects"
    nested = allowed_root / "my-app"
    nested.mkdir(parents=True)
    driver = ClaudeCodeDriver(allowed_working_directories=[str(allowed_root)])
    assert driver._resolve_cwd(str(nested)) == str(nested.resolve())


def test_resolve_cwd_rejects_paths_outside_whitelist(tmp_path):
    allowed_root = tmp_path / "projects"
    allowed_root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    driver = ClaudeCodeDriver(allowed_working_directories=[str(allowed_root)])
    with pytest.raises(RuntimeError, match="outside the allowed roots"):
        driver._resolve_cwd(str(outside))


def test_resolve_cwd_unrestricted_when_no_whitelist_configured(tmp_path):
    driver = ClaudeCodeDriver()
    assert driver._resolve_cwd(str(tmp_path)) == str(tmp_path.resolve())


def test_interrupt_without_active_process_is_noop():
    driver = ClaudeCodeDriver()
    asyncio.run(driver.interrupt("no-such-session"))


def test_run_session_times_out_and_interrupts_hung_process():
    driver = ClaudeCodeDriver(timeout_seconds=0.05)
    process = _HangingProcess()
    published = []

    async def publish(event):
        published.append(event)

    async def scenario():
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            session = {"session_id": "sess_1", "working_directory": None}
            body = {"messages": [{"role": "user", "content": "hang please"}]}
            return await driver.run_session(session, body, publish)

    message, updates = asyncio.run(scenario())

    assert process.sent_signals, "interrupt should have signaled the hung process"
    assert message["stop_reason"] == "timeout"
    assert message["content"] == []
    assert updates["status"] == "interrupted"
    assert any(event["type"] == "session_interrupted" for event in published)
    assert "sess_1" not in driver._active
