import shutil
import uuid
from typing import Any

from .base import make_event
from .subprocess_agent import SubprocessAgentDriver, probe_version

_MODEL_ALIASES = ("default", "opus", "sonnet", "fable")


class ClaudeCodeDriver(SubprocessAgentDriver):
    """Drives the `claude` CLI in non-interactive (--print) mode.

    Rather than attaching to a raw PTY (unavailable on Windows without extra
    native dependencies), this uses `--output-format stream-json
    --include-partial-messages`, which gives structured, real-time agentic
    events (text deltas, thinking, tool_use, tool_result) directly as NDJSON
    on stdout. Multi-turn conversations are resumed with `--resume
    <claude_session_id>`, which the CLI itself persists to disk, so history
    survives a gateway restart.
    """

    name = "claude-code"

    async def detect(self) -> dict[str, Any]:
        if not shutil.which("claude"):
            return {"available": False, "status": "not_installed"}
        return {"available": True, "status": "idle", "version": await probe_version("claude")}

    async def models(self) -> list[dict[str, Any]]:
        if not (await self.detect())["available"]:
            return []
        return [
            {"id": f"claude-code:{alias}", "provider": self.name, "name": f"Claude Code ({alias})",
             "capabilities": ["reasoning", "coding", "agentic"]}
            for alias in _MODEL_ALIASES
        ]

    def _build_command(self, body: dict[str, Any], claude_session_id: str | None) -> list[str]:
        prompt = self._extract_prompt(body.get("messages", []))
        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--include-partial-messages"]

        model = body.get("model", "")
        if ":" in model:
            model = model.split(":", 1)[1]
        if model and model != "default":
            cmd += ["--model", model]

        if claude_session_id:
            cmd += ["--resume", claude_session_id]

        system_prompt = body.get("system")
        if isinstance(system_prompt, str) and system_prompt:
            cmd += ["--append-system-prompt", system_prompt]

        permission_mode = body.get("permission_mode")
        if permission_mode:
            cmd += ["--permission-mode", permission_mode]

        return cmd

    def _translate(
        self, line: dict[str, Any], session_id: str, claude_session_id: str | None
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
        events: list[dict[str, Any]] = []
        result_payload: dict[str, Any] | None = None
        line_type = line.get("type")

        if line_type == "system" and line.get("subtype") == "init":
            claude_session_id = line.get("session_id", claude_session_id)
            events.append(make_event(session_id, "session_started", {
                "model": line.get("model"), "cwd": line.get("cwd"), "claude_session_id": claude_session_id,
            }))
        elif line_type == "stream_event":
            inner = line.get("event", {})
            inner_type = inner.get("type")
            if inner_type == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    events.append(make_event(session_id, "assistant_message", {"text": delta.get("text", ""), "partial": True}))
                elif delta.get("type") == "thinking_delta":
                    events.append(make_event(session_id, "assistant_thinking", {"text": delta.get("thinking", ""), "partial": True}))
            elif inner_type == "content_block_start":
                block = inner.get("content_block", {})
                if block.get("type") == "tool_use":
                    events.append(make_event(session_id, "tool_started", {"id": block.get("id"), "name": block.get("name")}))
        elif line_type == "user":
            for item in line.get("message", {}).get("content", []):
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    events.append(make_event(session_id, "tool_output", {
                        "tool_use_id": item.get("tool_use_id"), "content": item.get("content"),
                        "is_error": item.get("is_error", False),
                    }))
        elif line_type == "result":
            result_payload = line
            events.append(make_event(session_id, "session_completed", {
                "result": line.get("result"), "is_error": line.get("is_error"), "usage": line.get("usage"),
                "cost_usd": line.get("total_cost_usd"), "duration_ms": line.get("duration_ms"),
                "num_turns": line.get("num_turns"),
            }))
            if line.get("is_error"):
                events.append(make_event(session_id, "error", {"message": line.get("result") or "claude reported an error"}))

        return events, claude_session_id, result_payload

    def _build_result_message(
        self, body: dict[str, Any], result_payload: dict[str, Any], accumulated_text: str
    ) -> tuple[dict[str, Any], str]:
        text = result_payload.get("result", accumulated_text)
        usage = result_payload.get("usage", {})
        message = {
            "id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant",
            "model": body.get("model", self.name), "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)},
            "stop_reason": "error" if result_payload.get("is_error") else "end_turn", "stop_sequence": None,
        }
        status = "error" if result_payload.get("is_error") else "idle"
        return message, status
