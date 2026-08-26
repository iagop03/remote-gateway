import asyncio
import json
from unittest.mock import AsyncMock, patch

from remote_gateway.drivers.codex import CodexDriver


class _FakeLinesProcess:
    """Replays fixed NDJSON lines on stdout, like a finished `codex exec --json` run."""

    def __init__(self, lines: list[dict]) -> None:
        self._lines = [json.dumps(line).encode() + b"\n" for line in lines]
        self.returncode = 0
        self.stdout = self
        self.stderr = self

    async def __aiter__(self):
        for line in self._lines:
            yield line

    async def read(self) -> bytes:
        return b""

    async def wait(self) -> int:
        return 0


def test_translate_thread_started_captures_thread_id():
    driver = CodexDriver()
    line = {"type": "thread.started", "thread_id": "abc-123"}
    events, thread_id, result = driver._translate(line, "sess_1", None)
    assert thread_id == "abc-123"
    assert result is None
    assert events[0]["type"] == "session_started"
    assert events[0]["data"]["thread_id"] == "abc-123"


def test_translate_agent_message_is_marked_non_partial():
    """Codex items arrive whole, not as deltas — partial must be False so the
    shared accumulator replaces instead of concatenating consecutive messages."""
    driver = CodexDriver()
    line = {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "Hi"}}
    events, _, _ = driver._translate(line, "sess_1", None)
    assert events[0]["type"] == "assistant_message"
    assert events[0]["data"] == {"text": "Hi", "partial": False}


def test_translate_command_execution_started_and_completed():
    driver = CodexDriver()
    started = {"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "ls"}}
    events, _, _ = driver._translate(started, "sess_1", None)
    assert events[0]["type"] == "tool_started"
    assert events[0]["data"] == {"id": "item_1", "name": "shell", "command": "ls"}

    completed = {"type": "item.completed", "item": {
        "id": "item_1", "type": "command_execution", "aggregated_output": "a.txt\n", "exit_code": 0, "status": "completed",
    }}
    events, _, _ = driver._translate(completed, "sess_1", None)
    assert events[0]["type"] == "tool_output"
    assert events[0]["data"] == {"tool_use_id": "item_1", "output": "a.txt\n", "exit_code": 0, "status": "completed"}


def test_translate_turn_completed_emits_session_completed():
    driver = CodexDriver()
    line = {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 4}}
    events, _, result = driver._translate(line, "sess_1", "abc-123")
    assert result is line
    assert events[0]["type"] == "session_completed"
    assert events[0]["data"]["usage"]["output_tokens"] == 4


def test_build_result_message_uses_last_complete_message_not_concatenation():
    """The agent may say several things before its final answer — the last
    agent_message is the response, earlier ones are narration-only events."""
    driver = CodexDriver()
    result_payload = {"usage": {"input_tokens": 5, "output_tokens": 2}}
    message, status = driver._build_result_message({"model": "codex:default"}, result_payload, "DONE")
    assert status == "idle"
    assert message["content"] == [{"type": "text", "text": "DONE"}]
    assert message["stop_reason"] == "end_turn"


def test_build_command_first_turn_vs_resume():
    driver = CodexDriver()
    first = driver._build_command({"messages": [{"role": "user", "content": "hi"}]}, None)
    assert first == ["codex", "exec", "hi", "--json", "--skip-git-repo-check"]

    resumed = driver._build_command({"messages": [{"role": "user", "content": "hi again"}]}, "thread-1")
    assert resumed[:5] == ["codex", "exec", "resume", "thread-1", "hi again"]
    assert "--json" in resumed and "--skip-git-repo-check" in resumed


def test_run_session_final_text_is_last_message_not_all_messages_concatenated():
    """End-to-end regression test for the accumulation bug: a turn where the
    agent says something before a tool call ("I'll check...") and something
    after ("DONE") must resolve to "DONE", not "I'll check...DONE"."""
    driver = CodexDriver()
    process = _FakeLinesProcess([
        {"type": "thread.started", "thread_id": "abc-123"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "I'll check the files."}},
        {"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "ls"}},
        {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "aggregated_output": "a.txt\n", "exit_code": 0, "status": "completed"}},
        {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": "DONE"}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
    ])
    published = []

    async def publish(event):
        published.append(event)

    async def scenario():
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            session = {"session_id": "sess_1", "working_directory": None}
            body = {"messages": [{"role": "user", "content": "list files then say done"}]}
            return await driver.run_session(session, body, publish)

    message, updates = asyncio.run(scenario())

    assert message["content"] == [{"type": "text", "text": "DONE"}]
    assert updates["driver_session_id"] == "abc-123"
    assert [e["type"] for e in published] == [
        "session_started", "assistant_message", "tool_started", "tool_output", "assistant_message", "session_completed",
    ]


def test_build_command_forwards_model_and_approve_for_me():
    driver = CodexDriver()
    cmd = driver._build_command({
        "messages": [{"role": "user", "content": "hi"}], "model": "codex:o3", "approve_for_me": True,
    }, None)
    assert cmd[cmd.index("--model") + 1] == "o3"
    assert "--approve-for-me" in cmd
