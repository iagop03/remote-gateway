"""`python -m remote_gateway doctor` — checks driver availability and config
safety without starting the server. Probes every known driver's CLI/service
regardless of its own *_ENABLED flag, so it can tell "installed but disabled
in config" apart from "not installed at all"."""
import asyncio
import sys

from ..config import LOCAL_HOSTS, Settings, get_settings
from ..drivers import ClaudeCodeDriver, CodexDriver, GeminiDriver, HTTPDriver


def _probe_drivers(settings: Settings) -> dict[str, object]:
    return {
        "claude-code": ClaudeCodeDriver(),
        "gemini": GeminiDriver(),
        "codex": CodexDriver(),
        "ollama": HTTPDriver("ollama", settings.ollama_base_url),
        "lmstudio": HTTPDriver("lmstudio", settings.lmstudio_base_url),
        "vllm": HTTPDriver("vllm", settings.vllm_base_url),
        "localai": HTTPDriver("localai", settings.localai_base_url),
    }


def _enabled_flags(settings: Settings) -> dict[str, bool]:
    return {
        "claude-code": settings.claude_code_enabled, "gemini": settings.gemini_enabled,
        "codex": settings.codex_enabled,
        "ollama": settings.ollama_enabled, "lmstudio": settings.lmstudio_enabled,
        "vllm": settings.vllm_enabled, "localai": settings.localai_enabled,
    }


async def run_doctor(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    print("Remote Gateway Doctor\n")
    all_ok = True

    py_ok = sys.version_info >= (3, 12)
    version_str = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(f"{'✓' if py_ok else '✗'} Python {version_str}" + ("" if py_ok else " — 3.12+ required"))
    all_ok = all_ok and py_ok

    enabled = _enabled_flags(settings)
    for name, driver in _probe_drivers(settings).items():
        status = await driver.detect()
        available = status["available"]
        symbol = "✓" if available else "✗"
        if available:
            detail = f"v{status['version']}" if status.get("version") else status.get("status", "available")
        else:
            detail = status.get("reason") or status.get("status") or "not available"
        note = "" if enabled[name] else "  [disabled in config]"
        print(f"{symbol} {name}: {detail}{note}")
        if enabled[name] and not available:
            all_ok = False  # something the operator turned on isn't actually usable

    print()
    host_is_local = settings.host in LOCAL_HOSTS
    if not host_is_local and not settings.token:
        print(f"✗ security: REMOTE_GATEWAY_HOST={settings.host!r} is not localhost and "
              "REMOTE_GATEWAY_TOKEN is empty — the server will refuse to start")
        all_ok = False
    elif not settings.token:
        print("✓ security: no token set, but host is localhost-only — fine for local use")
    else:
        print("✓ security: REMOTE_GATEWAY_TOKEN is set")

    print()
    print("All checks passed." if all_ok else "Some checks failed — see ✗ above.")
    return all_ok


def main() -> None:
    # Windows consoles often default to a legacy codepage (cp1252) that can't
    # encode ✓/✗ — verified live: printing them without this raises
    # UnicodeEncodeError and crashes before any output is even flushed.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ok = asyncio.run(run_doctor())
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
