# Remote Gateway

[![tests](https://github.com/iagop03/remote-gateway/actions/workflows/tests.yml/badge.svg)](https://github.com/iagop03/remote-gateway/actions/workflows/tests.yml)

Remote Gateway exposes local LLM providers through a small, authenticated FastAPI service. It supports driver discovery, model discovery, Anthropic-style `/v1/messages` responses, real SSE streaming, SQLite-backed sessions with event replay, an audit log, and operational metrics.

## Run

```powershell
.\start.ps1
```

Creates `.venv` and `.env` on first run if missing, then starts the server on
`http://127.0.0.1:9000`. Pass `-Port` / `-BindHost` to override.

Manual equivalent:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[test]"
copy .env.example .env
uvicorn remote_gateway.main:app --host 127.0.0.1 --port 9000
```

OpenAPI is available at `http://127.0.0.1:9000/docs`.

### Chatting with it

```powershell
python scripts\chat.py
```

Opens a session against the `claude-code` driver in your current directory
and drops you into a REPL — the same shape of interaction as the Anthropic
API (`messages` in, `content` blocks out), just routed through your local
driver instead of `api.anthropic.com`. Ctrl+C interrupts the current turn
without killing the REPL. Point it elsewhere with `--driver`, `--model`,
`--dir`, `--base-url`, `--token`.

If you're calling the API directly instead (curl, `Invoke-RestMethod`, the
`anthropic` SDK pointed at a custom `base_url`), responses are UTF-8 with an
explicit `charset=utf-8` on `Content-Type` — needed for Windows PowerShell
5.1's `Invoke-RestMethod`, which otherwise misdecodes non-ASCII text as
ISO-8859-1.

Models are addressed with a provider prefix, for example `ollama:mistral`,
`localai:<model>` or `claude-code:opus`. HTTP providers (`ollama`, `lmstudio`,
`vllm`, `localai`) are forwarded to their local service — `localai` is another
OpenAI-compatible target, so it's just another `HTTPDriver` config entry, no
new code.

**Verification status**: `ollama` is verified live (real streaming, real
turns, normalization tests). `lmstudio`, `vllm`, and `localai` run the exact
same `HTTPDriver` code path and OpenAI-compatible request/response shape —
same code, not a separate implementation — but none of the three has been
exercised against a real running instance. If you hit something that doesn't
work with one of them, that's why; open an issue with the actual response
shape your server sent.

### CLI agent drivers (Claude Code, Gemini CLI, Codex CLI)

All three subclass `SubprocessAgentDriver` ([drivers/subprocess_agent.py](remote_gateway/drivers/subprocess_agent.py)),
which owns everything CLI-agnostic: spawning one subprocess per turn, resolving
the executable (handles Windows npm `.cmd` shims, which `asyncio` doesn't find
on its own), enforcing a timeout, resolving `working_directory` (with an
optional whitelist), and interrupting gracefully. A subclass only implements
`_build_command`, `_translate` (its output format → internal events) and
`_build_result_message`. `POST /sessions` reports `can_interrupt: true` for
any driver that is a `SubprocessAgentDriver` — never hardcoded by name.

(An Aider driver was also built and dropped — same base class, but Aider has
no structured output mode and no auth of its own, and once every provider you
actually care about is configured directly, it added nothing you couldn't
already do with the API key you'd be pointing it at anyway.)

- `POST /sessions` creates a session (`driver: "claude-code"`, `"gemini"` or
  `"codex"`, optional `working_directory`).
- `POST /sessions/{id}/messages` sends one turn. The first turn starts a new
  CLI conversation; its own session id is captured from the first output line
  and persisted as `driver_session_id`, so subsequent turns resume it —
  conversation history survives a gateway restart because the CLI itself
  persists it to disk.
- Events (`session_started`, `assistant_message`, `tool_started`,
  `tool_output`, `session_completed`, `error`, and `assistant_thinking` for
  Claude Code) are published to `GET/WS /sessions/{id}/events` as they stream,
  not after the turn ends.
- `POST /sessions/{id}/interrupt` sends `SIGINT` (POSIX) or `CTRL_BREAK_EVENT`
  (Windows, via `CREATE_NEW_PROCESS_GROUP`) to the running process — never a
  kill. The turn ends with `stop_reason: "interrupted"` and the session stays
  usable for the next turn.
- `POST /sessions/{id}/stop` interrupts and marks the session `completed`.
- If the process produces no final result within `*_TIMEOUT_SECONDS` (default
  600), it's interrupted the same graceful way and the turn ends with
  `stop_reason: "timeout"` instead of hanging the request forever.

**Claude Code** (`CLAUDE_CODE_ENABLED=true` by default) runs `claude --print
--output-format stream-json --include-partial-messages` instead of attaching
to a raw PTY — Windows has no native `pty` module, and stream-json already
gives structured, real-time agentic events rather than requiring a
terminal-output parser. Text arrives as real token-level deltas
(`assistant_message` events with `partial: true`).

**Gemini CLI** (`GEMINI_ENABLED=false` by default — needs its own auth first:
`npm i -g @google/gemini-cli`, then `GEMINI_API_KEY` or OAuth login — note
Google discontinued "Sign in with Google" OAuth for individual accounts in
June 2026, use an API key from [aistudio.google.com](https://aistudio.google.com/apikey))
runs `gemini -p "<prompt>" -o stream-json --skip-trust`. Also real token-level
deltas (`partial: true`). Model defaults to `"auto"`; pass `gemini:<model>` to pin one.

**Codex CLI** (`CODEX_ENABLED=false` by default — needs `npm i -g @openai/codex`
then `codex login`) runs `codex exec --json` (`codex exec resume <id> ...` for
later turns — a subcommand, not a flag). Unlike the other two, Codex's JSONL
items arrive **whole**, not as token deltas — each `assistant_message` event
carries `partial: false`, which tells the shared accumulator to *replace*
rather than concatenate. This matters because Codex may emit several complete
messages in one turn (e.g. "I'll check the files..." before a tool call, then
the real answer after) — the final response is the *last* one, not all of them
glued together.

`/v1/messages` with `stream: true` streams real incremental deltas for every
driver — it reuses the same event flow as sessions instead of buffering the
whole reply and faking one SSE chunk. `ollama`/`lmstudio`/`vllm`/`localai`
stream from their native endpoints (NDJSON for Ollama, OpenAI-compatible SSE
for the rest) rather than a single blocking call.

### Audit log

Every completed turn (`/v1/messages`, streaming or not, and
`/sessions/{id}/messages`) is recorded to a SQLite `audit_log` table:
timestamp, `client_id`, driver, model, session id, input/output tokens,
origin IP, status. A caller self-identifies with an `X-Client-Id` header;
without one, entries are logged as `"anonymous"` rather than dropped, so
nothing goes unaccounted for.

```http
GET /logs?limit=100&client_id=platform-user-42&since=2026-08-01T00:00:00Z
```

This is simpler than [KeyBridge](../antcrew-proxy)'s audit log, which is a
hash-chained, tamper-evident JSONL file built for compliance. Remote
Gateway's is a plain queryable table — the ask here was "who consumed what,"
not WORM-grade immutability. If that's ever needed, it's a clear follow-up,
not a redesign.

### Operational metrics

```http
GET /metrics
{"uptime_seconds": 3600.0, "drivers": {
  "claude-code": {"sessions_active": 1, "messages_total": 25},
  "ollama": {"requests_total": 100, "avg_latency_ms": 1200.4}
}}
```

Distinct from `/logs` above — this is live operational state, not usage
history. Session-oriented drivers (Claude Code, Gemini, Codex) report
from the `sessions` table (a durable fact, not tracked twice in memory);
stateless HTTP drivers (Ollama, LM Studio, vLLM, LocalAI) self-report
request counts and average latency in-memory, since they have no session
rows to query.

### Session lifecycle limits

- `SESSION_TIMEOUT_MINUTES` (default 30) — a background sweep every 60s marks
  a session `expired` once it's gone this long without activity.
  `POST /sessions/{id}/messages` on an expired session returns `410` — create
  a new one.
- `MAX_CONCURRENT_SESSIONS` (default 10) — `POST /sessions` returns `429` once
  this many non-terminal sessions exist across all drivers.
- `ALLOW_MULTIPLE_SESSIONS_SAME_DRIVER` (default `false`) — `POST /sessions`
  returns `409` if an active session already exists for the requested driver;
  set to `true` to allow more than one concurrent session per driver.

"Active"/"non-terminal" means anything other than `completed`, `error`, or
`expired` — `interrupted` still counts, since an interrupted session is still
resumable.

### Rate limiting

`RATE_LIMIT_PER_MINUTE` (default 30, `0` disables it) — a sliding-window
limit per `X-Client-Id` on the model-invoking endpoints (`POST /v1/messages`,
`/sessions`, `/sessions/{id}/messages`). Every attempt counts, including one
the endpoint later rejects for its own reasons (`409` same-driver conflict,
`429` concurrency limit, ...) — this is about catching a runaway client, not
just successful calls. Deliberately far below KeyBridge's `PROXY_TOKEN_RPM`
default of 600: a local agent turn takes seconds to minutes, not the
milliseconds a thin proxy call to a cloud API does.

### Graceful shutdown

On shutdown, every in-flight session's subprocess is interrupted the same
graceful way `POST /sessions/{id}/interrupt` does (`SIGINT`/`CTRL_BREAK`,
never a kill) and given a grace period to exit before being force-killed as a
last resort. Before this fix, `lifespan` on shutdown only cancelled the
session reaper and closed the database — nothing touched a running
`claude`/`gemini`/`codex` subprocess, which would be left orphaned. Confirmed
by reading the old shutdown code (it genuinely did nothing to child
processes) and unit-tested against fakes matching the real
`asyncio.subprocess.Process` interface; not reproduced against a real
orphaned process live, since reliably delivering a graceful OS shutdown
signal to a background Windows process from this environment turned out to
be its own can of worms.

### CLI

```powershell
python -m remote_gateway doctor   # check driver availability & config safety, no server started
python -m remote_gateway start    # start the server (same as uvicorn remote_gateway.main:app)
remote-gateway doctor             # same, once pip-installed (console script)
```

`doctor` probes every known driver's CLI/service regardless of its own
`*_ENABLED` flag, so it distinguishes "installed but disabled in config" from
"not installed at all," and flags a driver that's enabled but not actually
available as a real failure (exit code 1). Verified live on Windows: printing
the ✓/✗ symbols crashes with `UnicodeEncodeError` on the legacy `cp1252`
console codepage unless stdout is explicitly reconfigured to UTF-8 first —
`doctor` does this itself.

### Docker

```powershell
docker compose up --build
```

Builds a self-contained image with Node.js + all three CLI agent drivers
(`@anthropic-ai/claude-code`, `@google/gemini-cli`, `@openai/codex`)
pre-installed — verified live with a real `docker build` + container run, all
three `--version` checks passing inside the container. Two things to know:

- **`REMOTE_GATEWAY_TOKEN` is required** — `docker-compose.yml` fails fast if
  it's unset, since the container binds `0.0.0.0` to publish the port, and the
  gateway's own startup check (above) refuses that without a token anyway.
- **CLI auth doesn't come with the image** — each tool logs in per-user
  (`~/.claude`, `~/.gemini`, `~/.codex`). Mount those directories as volumes
  (see `docker-compose.yml`) so login persists across container rebuilds, or
  `docker exec` in and log in fresh each time.

The `HEALTHCHECK` sends the same bearer token as every other endpoint —
without it, Docker would report a perfectly healthy container as unhealthy
on every check once a token is required (verified live: this is exactly what
happened before the fix).

### Structured logging

`LOG_FORMAT=console` (default, human-readable) or `json` (one object per
line, for log shipping). `LOG_LEVEL` as usual (`INFO`, `DEBUG`, ...). Logged:
startup (driver list, host/port, whether auth is required), session
create/expire, and turn failures — not every request, which uvicorn's own
access log already covers.

### Request/response validation

Every endpoint is typed with Pydantic models ([models/](remote_gateway/models/)) instead of
raw `dict[str, Any]` — malformed requests get a clean `422` instead of a
confusing failure deep inside a driver, and `/docs` now reflects real
schemas. `MessagesRequest` is deliberately loose on purpose
(`extra="allow"`, `model` optional): `HTTPDriver` forwards a client's entire
body verbatim to Ollama/LM Studio/vLLM/LocalAI, so fields like `temperature`,
`tools`, or OpenAI-style tool-role messages must survive validation
untouched rather than being stripped by a stricter schema. Verified live
that a request with unknown extra fields round-trips through
`model_dump()` unchanged.

### Security

- **The gateway refuses to start** if `REMOTE_GATEWAY_HOST` isn't
  `127.0.0.1`/`localhost`/`::1` and `REMOTE_GATEWAY_TOKEN` is empty — a
  service that can run shell commands via Claude Code must not be reachable
  from the network without auth.
- `ALLOWED_WORKING_DIRECTORIES` (comma-separated) optionally restricts which
  root directories `claude-code`/`gemini`/`codex` sessions may point
  `working_directory` at. Empty (default) means unrestricted, same as before.

Run tests with:

```powershell
pytest
```
