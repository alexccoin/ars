"""Configuration. Every value has a local-first default: A.R.S must boot and work with
no API keys, no network, and no accounts. Cloud is opt-in, always."""

from __future__ import annotations

import platform
import json
from pathlib import Path
from typing import Annotated

from ars_protocol import SUPPORTED_LANGUAGES, Language
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class VoiceConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARS_", env_file=".env", extra="ignore")

    wakeword: str = "hey_ars"
    wakeword_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    asr_model: str = "large-v3-turbo"
    asr_backend: str = Field(default_factory=lambda: default_asr_backend())
    """Which ASR implementation runs. Declared here, not in the voice service, because the
    deployment shape depends on it: a home node and a phone do not make the same choice.

    Defaults to `mlx-whisper` on Apple Silicon and `faster-whisper` everywhere else. That is
    not a preference, it is a measurement: CTranslate2 has no Metal backend, so on an M-series
    machine faster-whisper decodes large-v3-turbo on the CPU at 0.6-0.7x realtime — 8.2 s for
    a 5.9 s utterance against a 250 ms budget — while mlx-whisper does the same work on the
    GPU in 121 ms. Off Apple Silicon, faster-whisper is the portable path and stays the
    default."""
    asr_compute_type: str = "int8"
    """faster-whisper only. mlx-whisper carries its own dtype (float16 on Metal)."""
    endpoint_silence_ms: int = Field(default=700, ge=200, le=3000)
    """Silence before an utterance is considered finished. Too low interrupts the user
    mid-thought, which is worse than waiting. Tune against fixtures, never by feel."""
    tts_voice_en: str = "en_US-amy-medium"
    tts_voice_ro: str = "ro_RO-mihai-medium"
    tts_voice_de: str = "de_DE-thorsten-medium"


def default_asr_backend() -> str:
    """Apple Silicon gets the GPU backend; everything else gets the portable one."""
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return "mlx-whisper"
    return "faster-whisper"


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
    languages: Annotated[tuple[Language, ...], NoDecode] = SUPPORTED_LANGUAGES
    """Defaults to the protocol's set, never to a copy of it.

    This was a hard-coded `(EN, RO)`, and when German was added to
    `ars_protocol.SUPPORTED_LANGUAGES` the two silently disagreed: the gateway advertised
    two languages, and the document translator refused every German pair as an unsupported
    direction. CLAUDE.md rule 1 — a type defined twice is a bug — applies to the set of
    languages as much as to a message shape."""

    @field_validator("languages", mode="before")
    @classmethod
    def _parse_languages(cls, v: object) -> object:
        """Accept `ARS_LANGUAGES=en,ro`.

        pydantic-settings JSON-decodes complex types *before* validation, so the natural
        spelling in a .env file blows up at import with "error parsing value for field
        languages" — before anything has logged, so the user sees a stack trace instead of
        an assistant. `NoDecode` hands us the raw string so this runs at all. A config
        format that only accepts `["en","ro"]` is a config format nobody guesses.
        """
        if isinstance(v, str):
            text = v.strip()
            if text.startswith("["):
                # NoDecode turned off the automatic JSON parse, so honour it here — the
                # documented spelling must not stop working just because we added a
                # friendlier one.
                return tuple(json.loads(text))
            return tuple(part.strip() for part in text.split(",") if part.strip())
        return v

    companion_name: str = ""
    """A child's name. When A.R.S is asked about them it answers in a different voice and
    shows a different face — a seven-year-old and an adult want different things from the
    same assistant, and switching both is how a machine says "I am talking to you now".

    Empty by default and empty in the repository, because a child's name committed to a
    repository is a child's name published. Set it in .env."""

    listen_host: str = "127.0.0.1"
    """Which interface the gateway binds to.

    Loopback by default, and that default is the security model: A.R.S holds the user's
    documents and standing grants to act on their real accounts, so it is not reachable
    from a network until someone deliberately makes it so. Set to "0.0.0.0" to let other
    devices in — every one of them then needs the device token."""

    translate_documents: bool = True
    """Index an uploaded document in every language A.R.S speaks, not only its own.

    On, because it is what makes a German lease answer an English question: the document
    tier's embedding model cannot bridge languages, so the passage is bridged instead.
    Costs model time at upload — in the background, after the file is already answerable —
    and nothing on the hot path. Turn it off on a machine where the model is slow enough
    that a large upload would be churning for an hour."""

    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    guard: GuardConfig = Field(default_factory=GuardConfig)

    @property
    def default_language(self) -> Language:
        return self.languages[0]
