"""Voice-pipeline configuration.

`ars_core.VoiceConfig` owns every value that another service can also see
(`wakeword_threshold`, `endpoint_silence_ms`, `asr_model`, the two voice names). Those are
NOT redeclared here — they are composed in. This module only adds knobs that never leave
`services/voice`.
"""

from __future__ import annotations

from pathlib import Path

from ars_core import VoiceConfig
from ars_protocol import FRAME_MS, SAMPLE_RATE_HZ, AudioFormat, Language
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .vad.endpointing import DEFAULT_HESITATION_DE, DEFAULT_HESITATION_EN, DEFAULT_HESITATION_RO


class EndpointingConfig(BaseSettings):
    """Endpointing is the setting most likely to make A.R.S feel rude.

    Cutting the user off mid-thought is strictly worse than making them wait, so every
    default here errs toward waiting. Change nothing in this class without a before/after
    run of `ars-voice-endpoint-eval` on the same fixture set.
    """

    model_config = SettingsConfigDict(env_prefix="ARS_ENDPOINT_", env_file=".env", extra="ignore")

    min_utterance_ms: int = Field(default=320, ge=0)
    """Guard: never endpoint before this much *speech* has accumulated. Stops a cough or
    the tail of the wakeword itself from being endpointed as a complete utterance."""

    max_utterance_ms: int = Field(default=30_000, ge=1_000)
    """Hard stop. Something is wrong (open mic, TV in the room) rather than a long sentence."""

    speech_onset_ms: int = Field(default=100, ge=0)
    """Consecutive speech needed to declare SPEECH_START. Rejects single-frame blips."""

    resume_onset_ms: int = Field(default=60, ge=0)
    """Speech needed to cancel a running silence timer. Guards the countdown against a
    single noisy frame; see Endpointer.resume_onset_ms."""

    no_speech_timeout_ms: int = Field(default=6_000, ge=500)
    """The wakeword fired and nobody spoke. Close the microphone and go back to idle rather
    than hold an open mic on the strength of a false accept."""

    hesitation_grace_ms: int = Field(default=400, ge=0)
    """Extra silence granted when the utterance looks unfinished (trailing 'uhm', 'and',
    'si', a rising tail). The user is thinking, not done."""

    adaptive: bool = False
    """Adaptive endpointing: shorten the silence window for utterances that look complete.
    OFF by default — it trades a cut-off risk for latency and must be enabled only with
    fixture numbers in hand. See services/voice/README.md."""

    confident_silence_ms: int = Field(default=420, ge=150)
    """Silence window used when `adaptive` is on and the utterance looks complete."""

    trailing_hesitation_tokens_en: tuple[str, ...] = DEFAULT_HESITATION_EN
    trailing_hesitation_tokens_ro: tuple[str, ...] = DEFAULT_HESITATION_RO
    trailing_hesitation_tokens_de: tuple[str, ...] = DEFAULT_HESITATION_DE

    def hesitation_tokens(self, language: Language) -> tuple[str, ...]:
        return {
            Language.EN: self.trailing_hesitation_tokens_en,
            Language.RO: self.trailing_hesitation_tokens_ro,
            Language.DE: self.trailing_hesitation_tokens_de,
        }[language]


class VadConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARS_VAD_", env_file=".env", extra="ignore")

    backend: str = "energy"
    """'energy' (no weights, always available) or 'silero' (needs models/vad)."""

    energy_threshold_db: float = -42.0
    """dBFS above which a frame counts as speech for the energy VAD."""

    energy_adaptive: bool = True
    """Track the noise floor and float the threshold above it. Without this, the energy VAD
    is unusable in any room that is not a studio."""

    energy_noise_margin_db: float = 9.0
    energy_noise_floor_halflife_ms: int = 1_500
    silero_threshold: float = 0.5
    silero_model_dir: Path = Path("./models/vad")


class WakewordConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARS_WAKE_", env_file=".env", extra="ignore")

    backend: str = "mock"
    """'openwakeword' (needs models/wakeword), 'manual' (push-to-talk), 'mock' or 'null'."""

    pre_roll_ms: int = Field(default=500, ge=0, le=5_000)
    """Audio retained from before detection. Must match `WakeEvent.pre_roll_ms` semantics:
    without it the first syllable after the wakeword is clipped and ASR eats the verb."""

    refractory_ms: int = Field(default=1_500, ge=0)
    """Minimum gap between two detections. One utterance of 'hey A.R.S' crosses threshold
    on several consecutive frames; without this it fires three times."""

    strip_from_transcript: bool = True
    """Drop the wake phrase from the start of the final transcript.

    The pre-roll deliberately contains the tail of the keyword, so ASR transcribes it. On by
    default: without it every turn arrives at the reasoning layer with a stray vocative
    ("Jarvis, good morning..."), which then also ends up in memory."""

    model_dir: Path = Path("./models/wakeword")
    benchmark_dir: Path = Path("./research/benchmarks/wakeword")
    """Where the measured FA/hour lives. No file, no number — see WakewordEvaluation."""


class AsrConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARS_ASR_", env_file=".env", extra="ignore")

    # The backend name is NOT declared here: it lives in `ars_core.VoiceConfig.asr_backend`
    # and is reached through `VoicePipelineConfig.core`. Two fields bound to the same
    # ARS_ASR_BACKEND environment variable is the "type defined twice" defect from
    # CLAUDE.md #1, and it fails in the worst way — quietly, with the loser's default.

    model_dir: Path = Path("./models/asr")
    mlx_repo: str = "mlx-community/whisper-large-v3-turbo"
    """Overrides the repo derived from `core.asr_model`, for a fine-tuned local checkpoint."""

    mlx_dtype: str = "float16"
    device: str = "auto"
    """faster-whisper only."""
    beam_size_partial: int = 1
    beam_size_final: int = 5
    partial_interval_ms: int = Field(default=480, ge=FRAME_MS)
    """How often a partial re-decode runs. Lower = more responsive UI, more CPU stolen
    from the final decode, which is the one on the latency budget."""

    partial_window_ms: int = Field(default=8_000, ge=1_000)
    language_switch_confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    """A detection below this will not flip the conversation language."""

    language_switch_min_words: int = Field(default=3, ge=1)
    """...unless the utterance is at least this many words, in which case a weaker
    detection is trusted. 'da' and 'no' are not evidence of a language switch."""

    language_switch_min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    vad_filter: bool = True


class TtsConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARS_TTS_", env_file=".env", extra="ignore")

    backend: str = "mock"
    """'piper' (needs models/tts) or 'mock'."""

    model_dir: Path = Path("./models/tts")
    chunk_ms: int = Field(default=120, ge=FRAME_MS)
    """Synthesis chunk size. Also the granularity at which cancel() is observed."""

    first_sentence_max_chars: int = Field(default=90, ge=20)
    """Emit at the first sentence boundary, or this many characters, whichever comes first.

    This is a latency knob, not a formatting one. Piper renders a whole sentence before it
    emits a single sample, so time-to-first-audio scales with the length of the first
    sentence — measured at roughly 0.9 ms per character, on both voices:

    | first sentence | EN | RO |
    |---:|---:|---:|
    | 60 chars | 56 ms | 60 ms |
    | 80 chars | 74 ms | 73 ms |
    | 100 chars | 92 ms | 96 ms |
    | 140 chars | 119 ms | 124 ms |

    The budget is 120 ms, so the old 140-char default sat on top of it (128-132 ms measured
    end to end through the streaming synthesiser) and went over on a busy machine. 90 chars
    lands at ~85 ms with headroom. It only bites when the first sentence has no boundary
    inside 90 characters; a normal short opening sentence is emitted whole, unchanged."""

    speaker_sample_rate_hz: int = SAMPLE_RATE_HZ


class BargeInConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARS_BARGEIN_", env_file=".env", extra="ignore")

    enabled: bool = True
    min_speech_ms: int = Field(default=180, ge=FRAME_MS)
    """Sustained user speech required to interrupt playback. Too low and A.R.S interrupts
    itself on speaker bleed; too high and the user has to shout over it."""

    energy_margin_db: float = 6.0
    """Extra dB above the VAD threshold demanded while the speaker is active, to discount
    acoustic echo of our own output."""

    resume_listening: bool = True
    """After a barge-in, go straight to LISTENING and capture the interrupting utterance —
    making the user say the wakeword again to correct A.R.S is hostile."""


class VoicePipelineConfig(BaseSettings):
    """Everything `services/voice` needs. `core` is the shared contract; the rest is ours."""

    model_config = SettingsConfigDict(env_prefix="ARS_VOICE_", env_file=".env", extra="ignore")

    core: VoiceConfig = Field(default_factory=VoiceConfig)
    wakeword: WakewordConfig = Field(default_factory=WakewordConfig)
    vad: VadConfig = Field(default_factory=VadConfig)
    endpointing: EndpointingConfig = Field(default_factory=EndpointingConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)
    barge_in: BargeInConfig = Field(default_factory=BargeInConfig)

    warm_up_on_start: bool = True
    """Load every model before `VoicePipeline.run` consumes its first frame.

    On by default: a cold Piper voice is ~355 ms and a cold whisper decode ~500 ms, and both
    land inside the user's first turn otherwise. Turn it off only where startup latency
    matters more than first-turn latency (a test, or a CLI that exits immediately)."""

    fixtures_dir: Path = Path("./data/fixtures")
    languages: tuple[Language, ...] = (Language.EN, Language.RO)

    @property
    def audio_format(self) -> AudioFormat:
        """The one format the pipeline speaks. Capture resamples to this, never the reverse."""
        return AudioFormat()

    def voice_for(self, language: Language) -> str:
        return {
            Language.EN: self.core.tts_voice_en,
            Language.RO: self.core.tts_voice_ro,
            Language.DE: self.core.tts_voice_de,
        }[language]
