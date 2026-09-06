"""A.R.S voice pipeline — wakeword, VAD/endpointing, ASR, TTS, orchestration.

Bilingual EN/RO. Local-first: every engine has a real on-device backend and a fully
functional mock, and the mocks are what make the pipeline runnable and testable with no
weights on disk.

Owns four rows of the latency budget in docs/architecture/overview.md: wakeword detection,
endpointing, ASR final, TTS time-to-first-audio. `LatencyRecorder` reports against them.
"""

from .asr import (
    BilingualScript,
    FasterWhisperEngine,
    LanguageArbiter,
    MockAsrEngine,
    ScriptedUtterance,
)
from .audio import NullSink, PacedSink, PreRollBuffer
from .config import VoicePipelineConfig
from .factory import build_asr, build_pipeline, build_tts, build_vad, build_wakeword
from .handler import EchoTurnHandler, TurnHandler
from .metrics import BUDGET_P95_MS, VOICE_OWNED, LatencyRecorder, Stage
from .pipeline import VoicePipeline
from .tts import MockTtsEngine, PiperTtsEngine, StreamingSynthesizer
from .vad import Endpointer, EndpointReason, EndpointState, EnergyVadEngine, SileroVadEngine
from .wakeword import (
    MockWakewordEngine,
    OpenWakeWordEngine,
    WakewordEvaluation,
)

__version__ = "0.1.0"

__all__ = [
    "BUDGET_P95_MS", "VOICE_OWNED", "BilingualScript", "EchoTurnHandler", "EndpointReason",
    "EndpointState", "Endpointer", "EnergyVadEngine", "FasterWhisperEngine",
    "LanguageArbiter", "LatencyRecorder", "MockAsrEngine", "MockTtsEngine",
    "MockWakewordEngine", "NullSink", "OpenWakeWordEngine", "PacedSink", "PiperTtsEngine",
    "PreRollBuffer", "ScriptedUtterance", "SileroVadEngine", "Stage", "StreamingSynthesizer",
    "TurnHandler", "VoicePipeline", "VoicePipelineConfig", "WakewordEvaluation", "build_asr",
    "build_pipeline", "build_tts", "build_vad", "build_wakeword",
]
