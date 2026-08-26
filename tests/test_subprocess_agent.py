import asyncio
from unittest.mock import patch

from remote_gateway.drivers.subprocess_agent import SubprocessAgentDriver, _resolve_executable


def test_resolve_executable_prefers_shutil_which_result():
    """Regression test: asyncio.create_subprocess_exec does not search PATHEXT on
    Windows, so a bare "gemini" raises FileNotFoundError even though shutil.which
    finds the npm-installed gemini.cmd shim. run_session must launch the resolved
    path, not the bare command name."""
    with patch("shutil.which", return_value="C:\\nvm4w\\nodejs\\gemini.cmd"):
        assert _resolve_executable("gemini") == "C:\\nvm4w\\nodejs\\gemini.cmd"


def test_resolve_executable_falls_back_to_bare_name_when_not_found():
    with patch("shutil.which", return_value=None):
        assert _resolve_executable("does-not-exist") == "does-not-exist"


class _ConcreteDriver(SubprocessAgentDriver):
    """Minimal concrete subclass — only shutdown()/interrupt() are under test here."""

    name = "fake"

    async def detect(self):
        return {"available": True, "status": "idle"}

    async def models(self):
        return []

    def _build_command(self, body, resume_id):
        return ["fake"]

    def _translate(self, line, session_id, resume_id):
        return [], resume_id, None

    def _build_result_message(self, body, result_payload, accumulated_text):
        return {}, "idle"


class _FakeProcess:
    def __init__(self, responds_to_interrupt: bool):
        self.returncode = None
        self._responds = responds_to_interrupt
        self.killed = False
        self.signals: list[int] = []

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        if self._responds:
            self.returncode = -2

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        if self._responds:
            return self.returncode
        while not self.killed:  # never exits on its own
            await asyncio.sleep(0.01)
        return self.returncode


def test_shutdown_interrupts_and_waits_for_responsive_process():
    driver = _ConcreteDriver()
    process = _FakeProcess(responds_to_interrupt=True)
    driver._active["sess_1"] = process

    asyncio.run(driver.shutdown(grace_seconds=2))

    assert process.signals  # was interrupted
    assert not process.killed  # exited on its own, no kill needed
    assert "sess_1" not in driver._active


def test_shutdown_kills_unresponsive_process_after_grace_period():
    """Regression test: without this, a subprocess that ignores the interrupt
    (or a CLI that doesn't handle SIGINT/CTRL_BREAK cleanly) would be orphaned
    once the gateway process itself exits."""
    driver = _ConcreteDriver()
    process = _FakeProcess(responds_to_interrupt=False)
    driver._active["sess_1"] = process

    asyncio.run(driver.shutdown(grace_seconds=0.05))

    assert process.signals  # interrupted gracefully first, not killed outright
    assert process.killed  # ...but didn't respond in time
    assert "sess_1" not in driver._active


def test_shutdown_with_no_active_sessions_is_a_noop():
    driver = _ConcreteDriver()
    asyncio.run(driver.shutdown())
