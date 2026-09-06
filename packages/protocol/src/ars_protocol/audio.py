"""Audio and the voice pipeline's event vocabulary.

One rule governs this module: the sample rate, frame size, and encoding are declared
HERE and imported everywhere. A hardcoded 16000 anywhere else in the codebase is a defect.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .common import Language, Model, now_ms

SAMPLE_RATE_HZ = 16_000
"""16 kHz mono. Every ASR and wakeword model we support expects this; resample at capture."""

FRAME_MS = 20
"""20 ms frames — 320 samples. Small enough for responsive VAD, large enough to be cheap."""

SAMPLES_PER_FRAME = SAMPLE_RATE_HZ * FRAME_MS // 1000
BYTES_PER_SAMPLE = 2  # int16 PCM
BYTES_PER_FRAME = SAMPLES_PER_FRAME * BYTES_PER_SAMPLE


class AudioEncoding(StrEnum):
    PCM_S16LE = "pcm_s16le"
    OPUS = "opus"


class AudioFormat(Model):
    """Declared once at stream open; frames afterwards carry no format of their own."""

    encoding: AudioEncoding = AudioEncoding.PCM_S16LE
    sample_rate_hz: int = SAMPLE_RATE_HZ
    channels: int = Field(default=1, ge=1, le=2)


class AudioFrame(Model):
    """A single frame of captured audio.

    `seq` is monotonic per stream so a gap is detectable — dropped frames degrade ASR
    silently otherwise, which is the worst kind of audio bug to chase.
    """

    seq: int = Field(ge=0)
    captured_at_ms: int = Field(default_factory=now_ms)
    pcm: bytes


class WakeEvent(Model):
    """The wakeword fired.

    Privacy invariant: no audio leaves the device before one of these. Anything that
    breaks that invariant is a security incident, not a bug — see security/threat-models.
    """

    keyword: str
    score: float = Field(ge=0.0, le=1.0)
    detected_at_ms: int = Field(default_factory=now_ms)
    pre_roll_ms: int = Field(default=500, ge=0)
    """Audio retained from *before* detection, so the first syllable is not clipped."""


class SpeechBoundary(StrEnum):
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"


class VadEvent(Model):
    """Voice-activity boundary. SPEECH_END is what triggers endpointing."""

    boundary: SpeechBoundary
    at_ms: int = Field(default_factory=now_ms)
    energy_db: float | None = None


class TranscriptSegment(Model):
    text: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class Transcript(Model):
    """ASR output. Partials stream continuously; exactly one final ends an utterance.

    `language` is detected per utterance, not per session — this household switches
    between English and Romanian mid-conversation and the assistant must follow.
    """

    text: str
    language: Language
    language_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    is_final: bool = False
    segments: tuple[TranscriptSegment, ...] = ()
    audio_duration_ms: int = Field(default=0, ge=0)


class SynthesisRequest(Model):
    """Text to speak. Cancellable: barge-in must reach the synthesiser, not just mute output."""

    text: str
    language: Language
    voice: str | None = None
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


class SynthesisChunk(Model):
    """A chunk of synthesised audio. Streamed so time-to-first-audio stays low."""

    seq: int = Field(ge=0)
    pcm: bytes
    is_final: bool = False
