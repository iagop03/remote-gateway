from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    """Deliberately loose: `content` and any extra fields (tool_call_id, name,
    ...) pass through untouched, since HTTPDriver forwards the whole request
    body verbatim to Ollama/LM Studio/vLLM/LocalAI, which may expect
    OpenAI-style tool-role messages this gateway doesn't otherwise interpret."""
    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = None


class MessagesRequest(BaseModel):
    """Validates the fields Remote Gateway itself reads (model, messages,
    stream, working_directory) while allowing everything else through
    unvalidated — temperature, tools, permission_mode, approval_mode,
    sandbox, approve_for_me, system, and any other driver- or
    upstream-specific field a caller sends. Strict validation here would
    silently break pass-through to whatever OpenAI-compatible server is on
    the other end of an HTTPDriver."""
    model_config = ConfigDict(extra="allow")

    # Required for /v1/messages (no session to fall back to); optional for
    # /sessions/{id}/messages, where an absent model falls back to the
    # session's own stored model — enforced by each endpoint, not here.
    model: str = ""
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    working_directory: str | None = None


class ContentBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str = ""


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class MessageResponse(BaseModel):
    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    model: str = ""
    content: list[ContentBlock] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    stop_reason: str | None = None
    stop_sequence: str | None = None
