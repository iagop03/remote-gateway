import shutil
import uuid
from typing import Any

from .base import make_event
from .subprocess_agent import SubprocessAgentDriver, probe_version


class CodexDriver(SubprocessAgentDriver):
    """Drives the `codex` CLI (OpenAI Codex CLI) via `codex exec --json`.

    Unlike Claude Code/Gemini, Codex's JSONL events are not token-level
    deltas — each `item.completed` agent_message carries a whole, finished
    message. So unlike the other two drivers, `assistant_message` events here
    are published with `partial: False`, which tells the shared
    SubprocessAgentDriver to treat the *last* one as the final answer rather
    than concatenating every message the agent said along the way (it may say
    several, e.g. "I'll check the files..." before a tool call, then the real
    answer after). Resuming a thread uses the `codex exec resume <id>`
    subcommand, not a flag.
    """

    name = "codex"

    async def detect(self) -> dict[str, Any]:
        if not shutil.which("codex"):
            return {"available": False, "status": "not_installed"}
        return {"available": True, "status": "idle", "version": await probe_version("codex")}

    async def models(self) -> list[dict[str, Any]]:
        if not (await self.detect())["available"]:
            return []
        return [{"id": "codex:default", "provider": self.name, "name": "Codex CLI (default model)",
                 "capabilities": ["reasoning", "coding", "agentic"]}]

    def _build_command(self, body: dict[str, Any], thread_id: str | None) -> list[str]:
        prompt = self._extract_prompt(body.get("messages", []))

        if thread_id:
            cmd = ["codex", "exec", "resume", thread_id, prompt, "--json", "--skip-git-repo-check"]
        else:
            cmd = ["codex", "exec", prompt, "--json", "--skip-git-repo-check"]

        model = body.get("model", "")
        if ":" in model:
            model = model.split(":", 1)[1]
        if model and model != "default":
            cmd += ["--model", model]

        if body.get("approve_for_me"):
            cmd += ["--approve-for-me"]
        elif body.get("sandbox"):
            cmd += ["--sandbox", body["sandbox"]]

        return cmd

    def _translate(
        self, line: dict[str, Any], session_id: str, thread_id: str | None
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
        events: list[dict[str, Any]] = []
        result_payload: dict[str, Any] | None = None
        line_type = line.get("type")

        if line_type == "thread.started":
            thread_id = line.get("thread_id", thread_id)
            events.append(make_event(session_id, "session_started", {"thread_id": thread_id}))
        elif line_type == "item.started":
            item = line.get("item", {})
            if item.get("type") == "command_execution":
                events.append(make_event(session_id, "tool_started", {
                    "id": item.get("id"), "name": "shell", "command": item.get("command"),
                }))
        elif line_type == "item.completed":
            item = line.get("item", {})
            item_type = item.get("type")
            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    events.append(make_event(session_id, "assistant_message", {"text": text, "partial": False}))
            elif item_type == "command_execution":
                events.append(make_event(session_id, "tool_output", {
                    "tool_use_id": item.get("id"), "output": item.get("aggregated_output"),
                    "exit_code": item.get("exit_code"), "status": item.get("status"),
                }))
            elif item_type == "reasoning":
                text = item.get("text", "")
                if text:
                    events.append(make_event(session_id, "assistant_thinking", {"text": text, "partial": False}))
        elif line_type == "turn.completed":
            result_payload = line
            events.append(make_event(session_id, "session_completed", {"usage": line.get("usage")}))
        elif line_type in ("turn.failed", "error"):
            result_payload = {"type": "turn.completed", "usage": {}, "is_error": True}
            detail = line.get("error", line)
            events.append(make_event(session_id, "session_completed", {"error": detail}))
            events.append(make_event(session_id, "error", {"message": str(detail)}))

        return events, thread_id, result_payload

    def _build_result_message(
        self, body: dict[str, Any], result_payload: dict[str, Any], accumulated_text: str
    ) -> tuple[dict[str, Any], str]:
        usage = result_payload.get("usage") or {}
        is_error = bool(result_payload.get("is_error"))
        message = {
            "id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant",
            "model": body.get("model", self.name), "content": [{"type": "text", "text": accumulated_text}],
            "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)},
            "stop_reason": "error" if is_error else "end_turn", "stop_sequence": None,
        }
        return message, "error" if is_error else "idle"
