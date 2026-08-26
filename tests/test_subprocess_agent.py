from unittest.mock import patch

from remote_gateway.drivers.subprocess_agent import _resolve_executable


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
