import asyncio
import json
import re
import shutil
import signal
import subprocess
import sys
import uuid
from abc import abstractmethod
from pathlib import Path
from typing import Any

from .base import EventPublisher, LocalLLMDriver, make_event


def _resolve_executable(name: str) -> str:
    # On Windows, npm-installed CLIs are .cmd shims. asyncio.create_subprocess_exec
    # does not search PATHEXT the way shutil.which does, so a bare "gemini" raises
    # FileNotFoundError even though the shim is on PATH — resolve it explicitly.
    return shutil.which(name) or name


_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")


async def probe_version(binary: str) -> str | None:
    """Best-effort `<binary> --version` for driver detect() calls.

    Different CLIs format this differently — "2.1.240 (Claude Code)", bare
    "0.57.0", "codex-cli 0.149.1" — so this pulls out the first semver-shaped
    token rather than assuming the version is always the first word.
    """
    resolved = shutil.which(binary)
    if not resolved:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            resolved, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        text = stdout.decode("utf-8", "replace").strip()
        match = _VERSION_PATTERN.search(text)
        return match.group(0) if match else (text.split(" ")[0] or None)
    except (OSError, asyncio.TimeoutError):
        return None


class SubprocessAgentDriver(LocalLLMDriver):
    """Shared process-management skeleton for CLI-based coding agents (Claude Code,
    Gemini CLI, ...): spawn one subprocess per turn, stream its output through a
    driver-specific translator, enforce a timeout, and interrupt gracefully
    (SIGINT/CTRL_BREAK, never a kill) instead of hanging the request forever.

    Subclasses implement only the parts specific to one CLI's interface:
    `_build_command` (how to invoke it for one turn), `_translate` (how to turn
    one line of its output into zero or more internal events, plus optionally a
    final result payload), and `_build_result_message` (how to turn that result
    payload into the Anthropic-shaped response).
    """

    def __init__(self, timeout_seconds: int = 600, allowed_working_directories: list[str] | None = None) -> None:
        self._active: dict[str, asyncio.subprocess.Process] = {}
        self._timeout_seconds = timeout_seconds
        self._allowed_roots = [Path(root).expanduser().resolve() for root in (allowed_working_directories or [])]

    async def messages(self, body: dict[str, Any]) -> dict[str, Any]:
        session = {"session_id": f"sess_{uuid.uuid4().hex}", "working_directory": body.get("working_directory")}

        async def _discard(_event: dict[str, Any]) -> None:
            return None

        result, _ = await self.run_session(session, body, _discard)
        return result

    async def run_session(
        self, session: dict[str, Any], body: dict[str, Any], publish: EventPublisher
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        session_id = session.get("session_id") or session["id"]
        cwd = self._resolve_cwd(session.get("working_directory"))
        cmd = self._build_command(body, session.get("driver_session_id"))
        cmd = [_resolve_executable(cmd[0]), *cmd[1:]]

        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise RuntimeError(f"Failed to launch {self.name} CLI: {exc}") from exc

        self._active[session_id] = process
        resume_id = session.get("driver_session_id")
        result_payload: dict[str, Any] | None = None
        accumulated_text = ""
        stderr_task = asyncio.create_task(process.stderr.read())

        async def _pump() -> None:
            nonlocal resume_id, result_payload, accumulated_text
            async for raw_line in process.stdout:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                events, resume_id, maybe_result = self._translate(parsed, session_id, resume_id)
                for event in events:
                    text = event["data"].get("text") if event["type"] == "assistant_message" else None
                    if text:
                        # partial=True (Claude Code, Gemini): a fragment of the current
                        # message, append it. partial=False (Codex): a whole standalone
                        # message already, it replaces — the *last* one is the answer,
                        # not a concatenation of every message the agent said along the way.
                        accumulated_text = accumulated_text + text if event["data"].get("partial", True) else text
                    await publish(event)
                if maybe_result is not None:
                    result_payload = maybe_result
            await process.wait()

        timed_out = False
        try:
            await asyncio.wait_for(_pump(), timeout=self._timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            await self.interrupt(session_id)
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        finally:
            self._active.pop(session_id, None)
        stderr_bytes = await stderr_task

        if timed_out:
            await publish(make_event(session_id, "session_interrupted", {
                "reason": f"{self.name} timed out after {self._timeout_seconds}s",
            }))
            return self._interrupted_message(body, "timeout"), {"driver_session_id": resume_id, "status": "interrupted"}

        if result_payload is None:
            interrupted = process.returncode not in (0, None)
            if interrupted:
                stderr_text = stderr_bytes.decode("utf-8", "replace").strip()
                await publish(make_event(session_id, "session_interrupted", {
                    "reason": stderr_text or f"{self.name} exited with code {process.returncode}",
                }))
            return self._interrupted_message(body, "interrupted"), {"driver_session_id": resume_id, "status": "interrupted"}

        message, status = self._build_result_message(body, result_payload, accumulated_text)
        # Estimate tokens from text length when the driver doesn't report them.
        # Gemini CLI may not populate stats; codex exec may omit usage on some versions.
        # ~4 chars per token is a reasonable approximation for any Latin-script text.
        usage = message.setdefault("usage", {})
        if not usage.get("input_tokens") and not usage.get("output_tokens"):
            try:
                prompt = self._extract_prompt(body.get("messages", []))
            except RuntimeError:
                prompt = ""
            usage["input_tokens"] = max(1, len(prompt) // 4)
            usage["output_tokens"] = max(1, len(accumulated_text) // 4)
            usage["usage_estimated"] = True
        return message, {"driver_session_id": resume_id, "status": status}

    async def interrupt(self, session_id: str | None = None) -> None:
        process = self._active.get(session_id) if session_id else None
        if process is None or process.returncode is not None:
            return
        try:
            if sys.platform == "win32":
                process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                process.send_signal(signal.SIGINT)
        except (ProcessLookupError, OSError):
            pass

    async def shutdown(self, grace_seconds: float = 5.0) -> None:
        """Called when the gateway process itself is shutting down. Without this,
        an in-flight turn's subprocess is orphaned: interrupt() alone only sends
        the signal, it doesn't wait for the process to actually exit before our
        own process does. Interrupts every still-running session gracefully
        first, then kills whatever hasn't exited within the grace period, so
        nothing is left behind."""
        processes = list(self._active.items())
        for session_id, _process in processes:
            await self.interrupt(session_id)
        for session_id, process in processes:
            try:
                await asyncio.wait_for(process.wait(), timeout=grace_seconds)
            except asyncio.TimeoutError:
                process.kill()
            self._active.pop(session_id, None)

    def _resolve_cwd(self, working_directory: str | None) -> str | None:
        if not working_directory:
            return None
        path = Path(working_directory).expanduser().resolve()
        if not path.is_dir():
            raise RuntimeError(f"working_directory does not exist: {path}")
        if self._allowed_roots and not any(path == root or root in path.parents for root in self._allowed_roots):
            raise RuntimeError(f"working_directory {path} is outside the allowed roots")
        return str(path)

    def _interrupted_message(self, body: dict[str, Any], stop_reason: str) -> dict[str, Any]:
        return {"id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant",
                "model": body.get("model", self.name), "content": [], "usage": {},
                "stop_reason": stop_reason, "stop_sequence": None}

    @staticmethod
    def _extract_prompt(messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages or []):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
        raise RuntimeError("No user message found in request")

    @abstractmethod
    def _build_command(self, body: dict[str, Any], resume_id: str | None) -> list[str]: ...

    @abstractmethod
    def _translate(
        self, line: dict[str, Any], session_id: str, resume_id: str | None
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]: ...

    @abstractmethod
    def _build_result_message(
        self, body: dict[str, Any], result_payload: dict[str, Any], accumulated_text: str
    ) -> tuple[dict[str, Any], str]:
        """Return (Anthropic-shaped message, session status) once a result line has been seen."""
        ...
