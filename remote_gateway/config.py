from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class Settings(BaseSettings):
    host: str = "127.0.0.1"
    port: int = 9000
    token: str = ""
    database_path: str = Field("./remote_gateway.db", validation_alias="DATABASE_PATH")
    claude_code_enabled: bool = Field(True, validation_alias="CLAUDE_CODE_ENABLED")
    claude_code_timeout_seconds: int = Field(600, validation_alias="CLAUDE_CODE_TIMEOUT_SECONDS")
    allowed_working_directories: str = Field("", validation_alias="ALLOWED_WORKING_DIRECTORIES")
    gemini_enabled: bool = Field(False, validation_alias="GEMINI_ENABLED")
    gemini_timeout_seconds: int = Field(600, validation_alias="GEMINI_TIMEOUT_SECONDS")
    codex_enabled: bool = Field(False, validation_alias="CODEX_ENABLED")
    codex_timeout_seconds: int = Field(600, validation_alias="CODEX_TIMEOUT_SECONDS")
    ollama_enabled: bool = Field(True, validation_alias="OLLAMA_ENABLED")
    ollama_base_url: str = Field("http://localhost:11434", validation_alias="OLLAMA_BASE_URL")
    lmstudio_enabled: bool = Field(False, validation_alias="LMSTUDIO_ENABLED")
    lmstudio_base_url: str = Field("http://localhost:1234/v1", validation_alias="LMSTUDIO_BASE_URL")
    vllm_enabled: bool = Field(False, validation_alias="VLLM_ENABLED")
    vllm_base_url: str = Field("http://localhost:8000/v1", validation_alias="VLLM_BASE_URL")
    localai_enabled: bool = Field(False, validation_alias="LOCALAI_ENABLED")
    localai_base_url: str = Field("http://localhost:8080/v1", validation_alias="LOCALAI_BASE_URL")
    session_timeout_minutes: int = Field(30, validation_alias="SESSION_TIMEOUT_MINUTES")
    max_concurrent_sessions: int = Field(10, validation_alias="MAX_CONCURRENT_SESSIONS")
    allow_multiple_sessions_same_driver: bool = Field(False, validation_alias="ALLOW_MULTIPLE_SESSIONS_SAME_DRIVER")
    # Per-client_id sliding-window limit on model-invoking endpoints (POST
    # /v1/messages, /sessions, /sessions/{id}/messages). Local agent turns are
    # much heavier than a typical API call, so the default is far below
    # KeyBridge's PROXY_TOKEN_RPM=600 — this is about catching a runaway
    # client, not shaping steady traffic. 0 disables it.
    rate_limit_per_minute: int = Field(30, validation_alias="RATE_LIMIT_PER_MINUTE")
    log_level: str = Field("INFO", validation_alias="LOG_LEVEL")
    log_format: str = Field("console", validation_alias="LOG_FORMAT")  # "console" or "json"

    model_config = SettingsConfigDict(env_prefix="REMOTE_GATEWAY_", env_file=".env", extra="ignore")

    def allowed_working_directories_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_working_directories.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
