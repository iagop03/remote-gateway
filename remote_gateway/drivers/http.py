import json
import time
import uuid
from typing import Any

import httpx

from .base import EventPublisher, LocalLLMDriver, make_event


class HTTPDriver(LocalLLMDriver):
    def __init__(self, name: str, base_url: str):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._last_latency_ms = 0.0
        self._requests_total = 0
        self._latency_sum_ms = 0.0

    def metrics(self) -> dict[str, Any]:
        avg = self._latency_sum_ms / self._requests_total if self._requests_total else 0.0
        return {"requests_total": self._requests_total, "avg_latency_ms": round(avg, 1)}

    def _record_latency(self, started: float) -> None:
        elapsed_ms = (time.perf_counter() - started) * 1000
        self._last_latency_ms = elapsed_ms
        self._latency_sum_ms += elapsed_ms
        self._requests_total += 1

    async def detect(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                response = await client.get(f"{self.base_url}/api/tags" if self.name == "ollama" else f"{self.base_url}/models")
            return {"available": response.is_success, "status": "ready" if response.is_success else "error"}
        except httpx.HTTPError as exc:
            return {"available": False, "status": "not_running", "reason": str(exc)}

    async def models(self) -> list[dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                endpoint = f"{self.base_url}/api/tags" if self.name == "ollama" else f"{self.base_url}/models"
                response = await client.get(endpoint)
                response.raise_for_status()
            payload = response.json()
            values = payload.get("models", payload.get("data", []))
            return [{"id": f"{self.name}:{item.get('name', item.get('id'))}", "provider": self.name,
                     "name": item.get("name", item.get("id")), "capabilities": ["chat"]} for item in values]
        except (httpx.HTTPError, ValueError):
            return []

    async def messages(self, body: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        model = body.get("model", "")
        model = model.split(":", 1)[1] if model.startswith(f"{self.name}:") else model
        payload = {**body, "model": model, "stream": False}
        endpoint = f"{self.base_url}/api/chat" if self.name == "ollama" else f"{self.base_url}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(endpoint, json=payload)
                response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"{self.name} request failed: {exc}") from exc
        self._record_latency(started)
        if self.name == "ollama" and "message" in result:
            return {"id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant",
                    "model": body.get("model", self.name), "content": [{"type": "text", "text": result["message"].get("content", "")}],
                    "usage": {}, "stop_reason": "end_turn", "stop_sequence": None}
        if "choices" in result:
            text = result["choices"][0].get("message", {}).get("content", "")
            raw_usage = result.get("usage", {})
            usage = {"input_tokens": raw_usage.get("prompt_tokens", raw_usage.get("input_tokens", 0)),
                     "output_tokens": raw_usage.get("completion_tokens", raw_usage.get("output_tokens", 0))}
            return {"id": result.get("id", f"msg_{uuid.uuid4().hex}"), "type": "message", "role": "assistant",
                    "model": body.get("model", self.name), "content": [{"type": "text", "text": text}],
                    "usage": usage, "stop_reason": "end_turn", "stop_sequence": None}
        return result

    async def run_session(
        self, session: dict[str, Any], body: dict[str, Any], publish: EventPublisher
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Streams real, incremental token deltas from the local service's native
        streaming endpoint (Ollama's NDJSON, or OpenAI-compatible SSE for LM Studio /
        vLLM) instead of buffering the whole reply before publishing anything."""
        session_id = session.get("session_id") or session.get("id")
        started = time.perf_counter()
        model = body.get("model", "")
        model = model.split(":", 1)[1] if model.startswith(f"{self.name}:") else model
        payload = {**body, "model": model, "stream": True}
        endpoint = f"{self.base_url}/api/chat" if self.name == "ollama" else f"{self.base_url}/chat/completions"

        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream("POST", endpoint, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if self.name == "ollama":
                            chunk = json.loads(line)
                            delta = chunk.get("message", {}).get("content", "")
                            if delta:
                                text_parts.append(delta)
                                await publish(make_event(session_id, "assistant_message", {"text": delta, "partial": True}))
                            if chunk.get("done"):
                                usage = {"input_tokens": chunk.get("prompt_eval_count", 0),
                                          "output_tokens": chunk.get("eval_count", 0)}
                        else:
                            if not line.startswith("data:"):
                                continue
                            data = line[len("data:"):].strip()
                            if data == "[DONE]":
                                break
                            chunk = json.loads(data)
                            delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if delta:
                                text_parts.append(delta)
                                await publish(make_event(session_id, "assistant_message", {"text": delta, "partial": True}))
                            chunk_usage = chunk.get("usage")
                            if chunk_usage:
                                usage = {"input_tokens": chunk_usage.get("prompt_tokens", 0),
                                          "output_tokens": chunk_usage.get("completion_tokens", 0)}
        except (httpx.HTTPError, ValueError) as exc:
            await publish(make_event(session_id, "error", {"message": f"{self.name} request failed: {exc}"}))
            raise RuntimeError(f"{self.name} request failed: {exc}") from exc

        self._record_latency(started)
        text = "".join(text_parts)
        await publish(make_event(session_id, "session_completed", {"result": text, "usage": usage}))
        message = {"id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant",
                   "model": body.get("model", self.name), "content": [{"type": "text", "text": text}],
                   "usage": usage, "stop_reason": "end_turn", "stop_sequence": None}
        return message, {"status": "idle"}
