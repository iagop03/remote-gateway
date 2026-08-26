# Remote Gateway in the AI Ecosystem

Remote Gateway is a local LLM driver server. It exposes a unified Anthropic-shaped
`/v1/messages` API in front of local tools and models — Claude Code, Gemini CLI,
Codex CLI, Ollama, LM Studio, vLLM, and LocalAI.

---

## Drivers

| Driver | Type | What it runs |
|---|---|---|
| `claude-code` | Subprocess | Claude Code CLI (`claude`) |
| `gemini` | Subprocess | Gemini CLI (`gemini`) |
| `codex` | Subprocess | OpenAI Codex CLI (`codex`) |
| `ollama` | HTTP | Ollama local server |
| `lmstudio` | HTTP | LM Studio local server |
| `vllm` | HTTP | vLLM inference server |
| `localai` | HTTP | LocalAI server |

---

## Standalone

```bash
pip install remote-gateway
remote-gateway start --drivers claude-code,ollama
```

Call it directly:

```python
import httpx

client = httpx.Client(
    base_url="http://localhost:9000",
    headers={"Authorization": "Bearer your-token"},
)
resp = client.post("/v1/messages", json={
    "model": "claude-code:default",
    "messages": [{"role": "user", "content": "Explain this repo"}],
    "working_directory": "/path/to/project",
})
```

---

## With KeyBridge (cloud + local failover)

[KeyBridge](https://github.com/iagop03/keybridge) routes `local:*` requests here.
This lets you define a failover chain that tries cloud providers first and falls
back to local models automatically.

```bash
# KeyBridge env
REMOTE_GATEWAY_URL=http://localhost:9000
FAILOVER_CHAIN=anthropic:claude-opus-5,openai:gpt-4o,local:claude-code:default
```

**Security**: Remote Gateway can execute shell commands via Claude Code and subprocess
drivers. Always set `REMOTE_GATEWAY_TOKEN` in any network-exposed deployment:

```bash
REMOTE_GATEWAY_TOKEN=your-secret-token remote-gateway start
```

Remote Gateway enforces this at startup — it refuses to bind to a non-localhost
address without a token configured.

The recommended topology for production:

```
Internet → KeyBridge (public, HTTPS + PROXY_TOKEN)
                │
                │ private network only
                ▼
           Remote Gateway (token-auth, not publicly reachable)
```

---

## With antcrew SDK

```python
from antcrew import build_llm

# Via KeyBridge failover (recommended)
llm = build_llm("local:claude-code:default", base_url="https://keybridge.example.com")

# Direct (local dev only)
llm = build_llm("claude-code:default", base_url="http://localhost:9000")
```

---

## Sessions vs stateless calls

Remote Gateway supports two interaction modes:

| Mode | Endpoint | When to use |
|---|---|---|
| Stateless | `POST /v1/messages` | Single-turn, no context needed |
| Session | `POST /sessions` + `POST /sessions/{id}/messages` | Multi-turn, context preserved across calls |

Sessions also expose a WebSocket at `/sessions/{id}/events` for real-time streaming.

---

## Observability

```bash
GET /health      # driver availability
GET /metrics     # uptime, sessions, message counts
GET /logs        # audit log (client_id, driver, tokens, origin_ip)
GET /drivers     # available/unavailable drivers
GET /models      # models reported by each driver
```

---

## Related projects

- [KeyBridge](https://github.com/iagop03/keybridge) — LLM key proxy; routes `local:*` here
- [antcrew](https://github.com/iagop03/antcrew) — multi-agent SDK
- [antcrew-platform](https://github.com/iagop03/antcrew-platform) — SaaS platform
