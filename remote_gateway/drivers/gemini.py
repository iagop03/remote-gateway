import shutil
import uuid
from typing import Any

from .base import make_event
from .subprocess_agent import SubprocessAgentDriver, probe_version


class GeminiDriver(SubprocessAgentDriver):
    """Drives the `gemini` CLI (Google Gemini CLI) in headless mode.

    Uses `-o stream-json`, which — unlike Claude Code's stream-json — has no
    final "full text" field on its `result` line, so the final message text is
    accumulated from the `assistant` message deltas as they stream (handled by
    the shared SubprocessAgentDriver). Sessions are resumed with `--resume
    <session_id>`, the session id Gemini CLI itself assigns and reports on its
    `init` line.
    """

    name = "gemini"

    async def detect(self) -> dict[str, Any]:
        if not shutil.which("gemini"):
            return {"available": False, "status": "not_installed"}
        return {"available": True, "status": "idle", "version": await probe_version("gemini")}

    async def models(self) -> list[dict[str, Any]]:
        if not (await self.detect())["available"]:
            return []
        return [{"id": "gemini:auto", "provider": self.name, "name": "Gemini CLI (auto model)",
                 "capabilities": ["reasoning", "coding", "agentic"]}]

    def _build_command(self, body: dict[str, Any], gemini_session_id: str | None) -> list[str]:
        prompt = self._extract_prompt(body.get("messages", []))
        # --skip-trust: this is a non-interactive headless call, there is no
        # terminal to answer the per-workspace trust prompt.
        cmd = ["gemini", "-p", prompt, "-o", "stream-json", "--skip-trust"]

        model = body.get("model", "")
        if ":" in model:
            model = model.split(":", 1)[1]
        if model and model != "auto":
            cmd += ["--model", model]

        if gemini_session_id:
            cmd += ["--resume", gemini_session_id]

        approval_mode = body.get("approval_mode")
        if approval_mode:
            cmd += ["--approval-mode", approval_mode]

        return cmd

    def _translate(
        self, line: dict[str, Any], session_id: str, gemini_session_id: str | None
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
        events: list[dict[str, Any]] = []
        result_payload: dict[str, Any] | None = None
        line_type = line.get("type")

        if line_type == "init":
            gemini_session_id = line.get("session_id", gemini_session_id)
            events.append(make_event(session_id, "session_started", {
                "model": line.get("model"), "gemini_session_id": gemini_session_id,
            }))
        elif line_type == "message" and line.get("role") == "assistant":
            text = line.get("content", "")
            if text:
                events.append(make_event(session_id, "assistant_message", {"text": text, "partial": True}))
        elif line_type == "tool_use":
            events.append(make_event(session_id, "tool_started", {
                "id": line.get("tool_id"), "name": line.get("tool_name"), "input": line.get("parameters"),
            }))
        elif line_type == "tool_result":
            events.append(make_event(session_id, "tool_output", {
                "tool_use_id": line.get("tool_id"), "status": line.get("status"), "content": line.get("result"),
            }))
        elif line_type == "result":
            result_payload = line
            events.append(make_event(session_id, "session_completed", {
                "status": line.get("status"), "stats": line.get("stats"),
            }))
            if line.get("status") != "success":
                events.append(make_event(session_id, "error", {
                    "message": f"gemini reported status={line.get('status')!r}",
                }))

        return events, gemini_session_id, result_payload

    def _build_result_message(
        self, body: dict[str, Any], result_payload: dict[str, Any], accumulated_text: str
    ) -> tuple[dict[str, Any], str]:
        stats = result_payload.get("stats", {})
        is_error = result_payload.get("status") != "success"
        message = {
            "id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant",
            "model": body.get("model", self.name), "content": [{"type": "text", "text": accumulated_text}],
            "usage": {"input_tokens": stats.get("input_tokens", 0), "output_tokens": stats.get("output_tokens", 0)},
            "stop_reason": "error" if is_error else "end_turn", "stop_sequence": None,
        }
        return message, "error" if is_error else "idle"
