FROM python:3.12-slim

# Node.js is needed for the npm-installed CLI drivers (Claude Code, Gemini CLI,
# Codex CLI). git is useful to them too (Claude Code/Codex may shell out to it).
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates git gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# CLI agent drivers — all optional. The gateway starts fine with none of these
# installed; each is only spawned if its own *_ENABLED=true (all default to
# false except claude-code). Installed here so the image is self-contained;
# comment this layer out for a leaner image that only proxies to
# Ollama/LM Studio/vLLM/LocalAI (those need no local binary, just network
# reachability).
RUN npm install -g @anthropic-ai/claude-code @google/gemini-cli @openai/codex

WORKDIR /app
COPY pyproject.toml ./
COPY remote_gateway ./remote_gateway
RUN pip install --no-cache-dir .

ENV REMOTE_GATEWAY_HOST=0.0.0.0 \
    REMOTE_GATEWAY_PORT=9000 \
    DATABASE_PATH=/data/remote_gateway.db
VOLUME ["/data"]
EXPOSE 9000

# REMOTE_GATEWAY_TOKEN is deliberately NOT set here — the gateway refuses to
# start on a non-local host (0.0.0.0, as set above) without one, and baking a
# default token into the image would defeat that check. Set it via
# docker-compose.yml or `docker run -e`.

# /health requires the same bearer token as every other endpoint once
# REMOTE_GATEWAY_TOKEN is set (mandatory here — see the note above) — the
# healthcheck must send it, or Docker would report a perfectly healthy
# container as unhealthy on every check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "\
import os, urllib.request; \
req = urllib.request.Request('http://localhost:9000/health', headers={'Authorization': 'Bearer ' + os.environ.get('REMOTE_GATEWAY_TOKEN', '')}); \
urllib.request.urlopen(req)" || exit 1

CMD ["python", "-m", "remote_gateway", "start"]
