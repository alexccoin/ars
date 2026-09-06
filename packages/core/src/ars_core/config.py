"""Configuration. Every value has a local-first default: A.R.S must boot and work with
no API keys, no network, and no accounts. Cloud is opt-in, always."""

from __future__ import annotations

from pathlib import Path

from ars_protocol import Language
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class VoiceConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARS_", env_file=".env", extra="ignore")

    wakeword: str = "hey_ars"
    wakeword_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    asr_model: str = "large-v3-turbo"
    asr_compute_type: str = "int8"
    endpoint_silence_ms: int = Field(default=700, ge=200, le=3000)
    """Silence before an utterance is considered finished. Too low interrupts the user
    mid-thought, which is worse than waiting. Tune against fixtures, never by feel."""
    tts_voice_en: str = "en_US-amy-medium"
    tts_voice_ro: str = "ro_RO-mihai-medium"


class LlmConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARS_", env_file=".env", extra="ignore")

    llm_backend: str = "local"
    llm_local_model: str = "qwen3:14b"
    llm_local_host: str = "http://127.0.0.1:11434"
    llm_cloud_model: str = "claude-sonnet-5"


class GuardConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARS_", env_file=".env", extra="ignore")

    guard_enabled: bool = True
    """Kill switch. False disables ALL private-data access — it does not disable checks.
    There is deliberately no setting that turns the guard off while keeping access on."""
    guard_audit_log: Path = Path("./var/audit.jsonl")
    consent_timeout_s: float = 60.0
    max_tool_calls_per_turn: int = 8
    """A runaway agent loop touching real accounts is the failure mode this prevents."""


class ArsConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARS_", env_file=".env", extra="ignore")

    env: str = "dev"
    data_dir: Path = Path("./var")
    log_level: str = "INFO"
    languages: tuple[Language, ...] = (Language.EN, Language.RO)

    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    guard: GuardConfig = Field(default_factory=GuardConfig)

    @property
    def default_language(self) -> Language:
        return self.languages[0]
