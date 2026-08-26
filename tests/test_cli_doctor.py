import asyncio
from unittest.mock import AsyncMock, patch

from remote_gateway.config import Settings
from remote_gateway.cli.doctor import run_doctor


def test_run_doctor_ok_when_enabled_drivers_are_available(capsys):
    settings = Settings(claude_code_enabled=True, ollama_enabled=False, token="secret")
    with patch("remote_gateway.cli.doctor.ClaudeCodeDriver.detect", new=AsyncMock(return_value={"available": True, "status": "idle", "version": "1.2.3"})), \
         patch("remote_gateway.cli.doctor.HTTPDriver.detect", new=AsyncMock(return_value={"available": False, "status": "not_running", "reason": ""})), \
         patch("remote_gateway.cli.doctor.GeminiDriver.detect", new=AsyncMock(return_value={"available": False, "status": "not_installed"})), \
         patch("remote_gateway.cli.doctor.CodexDriver.detect", new=AsyncMock(return_value={"available": False, "status": "not_installed"})):
        ok = asyncio.run(run_doctor(settings))

    out = capsys.readouterr().out
    assert ok is True  # only claude-code is enabled, and it's available — disabled/unavailable others don't fail the check
    assert "claude-code: v1.2.3" in out
    assert "[disabled in config]" in out  # gemini/codex/ollama are all disabled here


def test_run_doctor_fails_when_an_enabled_driver_is_unavailable():
    settings = Settings(claude_code_enabled=True, ollama_enabled=False)
    with patch("remote_gateway.cli.doctor.ClaudeCodeDriver.detect", new=AsyncMock(return_value={"available": False, "status": "not_installed"})), \
         patch("remote_gateway.cli.doctor.HTTPDriver.detect", new=AsyncMock(return_value={"available": False, "status": "not_running", "reason": ""})), \
         patch("remote_gateway.cli.doctor.GeminiDriver.detect", new=AsyncMock(return_value={"available": False, "status": "not_installed"})), \
         patch("remote_gateway.cli.doctor.CodexDriver.detect", new=AsyncMock(return_value={"available": False, "status": "not_installed"})):
        ok = asyncio.run(run_doctor(settings))

    assert ok is False  # claude-code is enabled but not actually available


def test_run_doctor_flags_insecure_non_local_host():
    settings = Settings(host="0.0.0.0", token="secret", claude_code_enabled=False, ollama_enabled=False)
    with patch("remote_gateway.cli.doctor.ClaudeCodeDriver.detect", new=AsyncMock(return_value={"available": False, "status": "not_installed"})), \
         patch("remote_gateway.cli.doctor.HTTPDriver.detect", new=AsyncMock(return_value={"available": False, "status": "not_running", "reason": ""})), \
         patch("remote_gateway.cli.doctor.GeminiDriver.detect", new=AsyncMock(return_value={"available": False, "status": "not_installed"})), \
         patch("remote_gateway.cli.doctor.CodexDriver.detect", new=AsyncMock(return_value={"available": False, "status": "not_installed"})):
        ok = asyncio.run(run_doctor(settings))  # host is non-local but token is set — should be fine
    assert ok is True
