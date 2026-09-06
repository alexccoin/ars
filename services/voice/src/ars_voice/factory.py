"""Engine construction from configuration.

Every engine is chosen by a config string and returned as its `packages/core` interface, so
the pipeline never knows whether it is talking to openWakeWord or a mock, faster-whisper or
a script. That is the whole point of the interfaces, and it is what makes the on-device and
cloud deployments the same codebase.

Backends are constructed but not loaded here: weights load on first use (or on an explicit
`load()`), so importing this module on a machine with no models is free.
"""

from __future__ import annotations

import logging

from ars_core import AsrEngine, TtsEngine, WakewordEngine

from .asr.faster_whisper_engine import FasterWhisperEngine
from .asr.language import LanguageArbiter
from .asr.mlx_whisper_engine import MlxWhisperEngine
from .asr.mock import MockAsrEngine, ScriptedUtterance
from .config import VoicePipelineConfig
from .tts.mock import MockTtsEngine, NullTtsEngine
from .tts.piper import PiperTtsEngine
from .vad.base import FrameVadEngine
from .vad.energy import EnergyVadEngine
from .vad.silero import SileroVadEngine
from .wakeword.mock import MockWakewordEngine, NullWakewordEngine
from .wakeword.openwakeword_engine import OpenWakeWordEngine

log = logging.getLogger(__name__)


def build_wakeword(config: VoicePipelineConfig, **overrides) -> WakewordEngine:
    cfg = config.wakeword
    common = dict(
        keyword=config.core.wakeword,
        threshold=config.core.wakeword_threshold,
        pre_roll_ms=cfg.pre_roll_ms,
        refractory_ms=cfg.refractory_ms,
        benchmark_dir=cfg.benchmark_dir,
    )
    common.update(overrides)
    match cfg.backend:
        case "openwakeword":
            return OpenWakeWordEngine(model_dir=cfg.model_dir, **common)
        case "mock":
            return MockWakewordEngine(**common)
        case "null":
            return NullWakewordEngine(**common)
        case other:
            raise ValueError(f"unknown wakeword backend {other!r} (openwakeword|mock|null)")


def build_vad(config: VoicePipelineConfig) -> FrameVadEngine:
    cfg = config.vad
    match cfg.backend:
        case "silero":
            return SileroVadEngine(threshold=cfg.silero_threshold, model_dir=cfg.silero_model_dir)
        case "energy":
            return EnergyVadEngine(
                threshold_db=cfg.energy_threshold_db,
                adaptive=cfg.energy_adaptive,
                noise_margin_db=cfg.energy_noise_margin_db,
                noise_floor_halflife_ms=cfg.energy_noise_floor_halflife_ms,
            )
        case other:
            raise ValueError(f"unknown VAD backend {other!r} (energy|silero)")


def build_asr(config: VoicePipelineConfig, *, script=None) -> AsrEngine:
    cfg = config.asr
    arbiter = LanguageArbiter(
        switch_confidence=cfg.language_switch_confidence,
        min_words_for_weak_switch=cfg.language_switch_min_words,
        weak_switch_confidence=cfg.language_switch_min_confidence,
        supported=config.languages,
    )
    match config.core.asr_backend:
        case "mlx-whisper":
            # Apple Silicon default. 121 ms EN / 135 ms RO on large-v3-turbo, measured;
            # faster-whisper is 8.2 s for the same audio on the same machine.
            return MlxWhisperEngine(
                repo=cfg.mlx_repo,
                model=config.core.asr_model,
                dtype=cfg.mlx_dtype,
                model_dir=cfg.model_dir,
                partial_interval_ms=cfg.partial_interval_ms,
                arbiter=arbiter,
            )
        case "faster-whisper":
            return FasterWhisperEngine(
                model=config.core.asr_model,
                compute_type=config.core.asr_compute_type,
                device=cfg.device,
                model_dir=cfg.model_dir,
                partial_interval_ms=cfg.partial_interval_ms,
                partial_window_ms=cfg.partial_window_ms,
                beam_size_partial=cfg.beam_size_partial,
                beam_size_final=cfg.beam_size_final,
                vad_filter=cfg.vad_filter,
                arbiter=arbiter,
            )
        case "mock":
            utterances = [
                item if isinstance(item, ScriptedUtterance) else ScriptedUtterance(text=item)
                for item in (script or ())
            ]
            return MockAsrEngine(utterances, arbiter=arbiter)
        case other:
            raise ValueError(
                f"unknown ASR backend {other!r} (mlx-whisper|faster-whisper|mock). "
                "Set ARS_ASR_BACKEND."
            )


def build_tts(config: VoicePipelineConfig) -> TtsEngine:
    cfg = config.tts
    match cfg.backend:
        case "piper":
            return PiperTtsEngine(
                voice_en=config.core.tts_voice_en,
                voice_ro=config.core.tts_voice_ro,
                model_dir=cfg.model_dir,
                chunk_ms=cfg.chunk_ms,
                first_sentence_max_chars=cfg.first_sentence_max_chars,
            )
        case "mock":
            return MockTtsEngine(
                chunk_ms=cfg.chunk_ms,
                voice_en=config.core.tts_voice_en,
                voice_ro=config.core.tts_voice_ro,
                first_sentence_max_chars=cfg.first_sentence_max_chars,
            )
        case "null":
            return NullTtsEngine()
        case other:
            raise ValueError(f"unknown TTS backend {other!r} (piper|mock|null)")


def build_pipeline(config: VoicePipelineConfig | None = None, **kwargs):
    """Assemble a pipeline from config. `kwargs` are passed to `VoicePipeline`."""
    from .pipeline import VoicePipeline

    config = config or VoicePipelineConfig()
    script = kwargs.pop("script", None)
    return VoicePipeline(
        wakeword=kwargs.pop("wakeword", None) or build_wakeword(config),
        vad=kwargs.pop("vad", None) or build_vad(config),
        asr=kwargs.pop("asr", None) or build_asr(config, script=script),
        tts=kwargs.pop("tts", None) or build_tts(config),
        config=config,
        **kwargs,
    )
