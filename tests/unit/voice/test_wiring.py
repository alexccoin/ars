"""Configuration, factory wiring, and the lazy-import rule.

`packages/core` says no vendor SDK may be imported outside a backend implementation. The
practical form of that rule for `services/voice` is: importing the package, building any
engine, and running the whole mock pipeline must never import faster-whisper, piper,
openwakeword, torch or sounddevice. This file asserts it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from ars_core import AsrEngine, TtsEngine, VadEngine, WakewordEngine
from ars_protocol import SAMPLE_RATE_HZ, AudioEncoding, Language
from ars_voice.config import VoicePipelineConfig
from ars_voice.factory import build_asr, build_pipeline, build_tts, build_vad, build_wakeword

VENDOR_MODULES = ("faster_whisper", "piper", "openwakeword", "torch", "sounddevice", "onnxruntime")


def test_engines_are_built_behind_the_core_interfaces():
    config = VoicePipelineConfig()
    assert isinstance(build_wakeword(config), WakewordEngine)
    assert isinstance(build_vad(config), VadEngine)
    assert isinstance(build_asr(config), AsrEngine)
    assert isinstance(build_tts(config), TtsEngine)


def test_real_backends_construct_without_weights_and_only_fail_on_use():
    """Constructing must be free; the weights load lazily on first use."""
    config = VoicePipelineConfig()
    config.wakeword.backend = "openwakeword"
    config.asr.backend = "faster-whisper"
    config.tts.backend = "piper"
    config.vad.backend = "silero"
    assert isinstance(build_wakeword(config), WakewordEngine)
    assert isinstance(build_asr(config), AsrEngine)
    assert isinstance(build_tts(config), TtsEngine)
    assert isinstance(build_vad(config), VadEngine)
    for module in VENDOR_MODULES:
        assert module not in sys.modules, f"{module} was imported at construction time"


def test_unknown_backends_fail_loudly():
    config = VoicePipelineConfig()
    config.asr.backend = "whisper.cpp"
    with pytest.raises(ValueError, match="unknown ASR backend"):
        build_asr(config)


def test_no_vendor_sdk_is_imported_by_a_full_mock_pipeline_run():
    script = """
import asyncio, sys
from ars_voice.factory import build_pipeline
from ars_voice.asr.mock import ScriptedUtterance
from ars_voice.audio.frames import frames_from_pcm
from ars_voice.audio.synth import keyword_marker_pcm, room_noise_pcm, speech_like_pcm
from ars_voice.eval.wakeword import marker_score
from ars_protocol import Language

pipeline = build_pipeline(script=[ScriptedUtterance("hello", Language.EN, 0.9)])
pipeline.wakeword._score_fn = marker_score
pcm = room_noise_pcm(300) + keyword_marker_pcm(480) + speech_like_pcm(900) + room_noise_pcm(1100)

async def frames():
    for f in frames_from_pcm(pcm):
        yield f

async def main():
    async for _ in pipeline.run(frames()):
        pass

asyncio.run(main())
vendors = [m for m in ("faster_whisper","piper","openwakeword","torch","sounddevice","onnxruntime")
           if m in sys.modules]
print(",".join(vendors))
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "", f"vendor SDK imported: {result.stdout.strip()}"


def test_the_audio_format_comes_from_the_protocol():
    config = VoicePipelineConfig()
    fmt = config.audio_format
    assert fmt.sample_rate_hz == SAMPLE_RATE_HZ
    assert fmt.encoding is AudioEncoding.PCM_S16LE
    assert fmt.channels == 1


def test_voice_selection_is_bilingual_and_comes_from_shared_config():
    config = VoicePipelineConfig()
    assert config.voice_for(Language.EN) == config.core.tts_voice_en
    assert config.voice_for(Language.RO) == config.core.tts_voice_ro
    assert config.voice_for(Language.RO).startswith("ro_RO")


def test_shared_settings_are_not_redeclared_in_the_voice_config():
    """`endpoint_silence_ms`, the thresholds and the voice names live in ars_core.VoiceConfig.
    A second copy in this service is the bug CLAUDE.md non-negotiable #1 describes."""
    source = (
        Path(__file__).resolve().parents[3]
        / "services" / "voice" / "src" / "ars_voice" / "config.py"
    ).read_text()
    for owned_by_core in (
        "endpoint_silence_ms:", "wakeword_threshold:", "asr_model:",
        "tts_voice_en:", "tts_voice_ro:", "asr_compute_type:",
    ):
        assert owned_by_core not in source, f"{owned_by_core} redeclared in services/voice"


def test_build_pipeline_assembles_a_runnable_default():
    pipeline = build_pipeline()
    assert pipeline.endpointer.silence_ms == float(pipeline.config.core.endpoint_silence_ms)
    assert pipeline.synthesizer.engine is pipeline.tts
