# Remote Gateway Changelog

## 1.0.0 (2026-08-26)

First stable release.

### Added

- **Three CLI agent drivers** — Claude Code, Gemini CLI, Codex — sharing a
  common `SubprocessAgentDriver` base: spawn-per-turn, configurable timeout,
  graceful `SIGINT`/`CTRL_BREAK` interrupt (never a kill), `working_directory`
  whitelist, and real-time streamed events (`assistant_message`,
  `tool_started`, `tool_output`, ...) instead of buffering a whole turn.
- **Four HTTP pass-through drivers** — Ollama, LM Studio, vLLM, LocalAI —
  with real native streaming (NDJSON for Ollama, OpenAI-compatible SSE for
  the rest). Only Ollama has been verified against a real running instance;
  the other three share its exact code path but are otherwise unverified.
- **Sessions**: create/resume/interrupt/stop, `MAX_CONCURRENT_SESSIONS`,
  `ALLOW_MULTIPLE_SESSIONS_SAME_DRIVER`, and an inactivity reaper
  (`SESSION_TIMEOUT_MINUTES`) that marks stale sessions `expired`.
- **Graceful shutdown** — every in-flight session's subprocess is interrupted
  and given a grace period before being force-killed, so a turn in progress
  when the gateway exits doesn't leave an orphaned child process.
- **Rate limiting** — sliding-window `RATE_LIMIT_PER_MINUTE` per `X-Client-Id`
  on the model-invoking endpoints.
- **Audit log** (`GET /logs`) — SQLite-backed, per-request client_id/driver/
  model/tokens/origin IP/status.
- **Operational metrics** (`GET /metrics`) — session counts from storage for
  agent drivers, in-memory request/latency stats for HTTP drivers.
- **Security**: refuses to start on a non-local host without
  `REMOTE_GATEWAY_TOKEN`; bearer auth via `hmac.compare_digest`.
- **Pydantic request/response validation** on every endpoint, with
  `extra="allow"` on the messages request so pass-through fields
  (`temperature`, `tools`, ...) survive untouched for `HTTPDriver`.
- **CLI** (`python -m remote_gateway doctor|start`, or `remote-gateway` once
  installed) — `doctor` probes every driver regardless of its `*_ENABLED`
  flag and flags an enabled-but-unavailable driver as a real failure.
- **Docker** — self-contained image with Node.js + all three CLI agent
  drivers pre-installed; `docker-compose.yml` requires `REMOTE_GATEWAY_TOKEN`
  and documents the volume mounts needed for each CLI's auth state.
- **Structured logging** via `structlog` (`LOG_FORMAT=console|json`).
- CI on GitHub Actions (Ubuntu + Windows), MIT license.

### Removed

- An Aider driver was built (same `SubprocessAgentDriver` base, with
  `_parse_line`/`_finalize` hooks added specifically for its lack of any
  structured output mode) and then removed before this release: once every
  provider actually in use is configured directly, it added nothing over
  pointing the same API key at those drivers already do.
