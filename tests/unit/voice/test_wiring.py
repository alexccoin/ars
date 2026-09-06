"""Configuration, factory wiring, and the lazy-import rule.

`packages/core` says no vendor SDK may be imported outside a backend implementation. The
practical form of that rule for `services/voice` is: importing the package, building any
engine, and running the whole mock pipeline must never import faster-whisper, piper,
openwakeword, torch or sounddevice. This file asserts it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from ars_core import AsrEngine, TtsEngine, VadEngine, WakewordEngine
from ars_protocol import SAMPLE_RATE_HZ, AudioEncoding, Language
from ars_voice.config import VoicePipelineConfig
from ars_voice.factory import build_asr, build_pipeline, build_tts, build_vad, build_wakeword

VENDOR_MODULES = (
    "faster_whisper", "mlx", "mlx_whisper", "piper", "openwakeword", "torch",
    "sounddevice", "onnxruntime",
)


def test_engines_are_built_behind_the_core_interfaces():
    config = VoicePipelineConfig()
    assert isinstance(build_wakeword(config), WakewordEngine)
    assert isinstance(build_vad(config), VadEngine)
    assert isinstance(build_asr(config), AsrEngine)
    assert isinstance(build_tts(config), TtsEngine)


@pytest.mark.parametrize("asr_backend", ["faster-whisper", "mlx-whisper"])
def test_real_backends_construct_without_weights_and_only_fail_on_use(asr_backend: str):
    """Constructing must be free; the weights load lazily on first use."""
    config = VoicePipelineConfig()
    config.wakeword.backend = "openwakeword"
    config.core.asr_backend = asr_backend
    config.tts.backend = "piper"
    config.vad.backend = "silero"
    # "no *new* vendor import", not "none loaded": tests/unit/voice/test_real_backends.py
    # legitimately loads mlx and piper earlier in the same process. The absolute version of
    # this assertion is `test_no_vendor_sdk_is_imported_by_a_full_mock_pipeline_run`, which
    # runs in a clean subprocess.
    before = {m for m in VENDOR_MODULES if m in sys.modules}
    assert isinstance(build_wakeword(config), WakewordEngine)
    assert isinstance(build_asr(config), AsrEngine)
    assert isinstance(build_tts(config), TtsEngine)
    assert isinstance(build_vad(config), VadEngine)
    after = {m for m in VENDOR_MODULES if m in sys.modules}
    assert after == before, f"imported at construction time: {sorted(after - before)}"


def test_unknown_backends_fail_loudly():
    config = VoicePipelineConfig()
    config.core.asr_backend = "whisper.cpp"
    with pytest.raises(ValueError, match="unknown ASR backend"):
        build_asr(config)


def test_the_asr_backend_is_declared_exactly_once():
    """`asr_backend` lives in ars_core.VoiceConfig. A second field in AsrConfig would bind to
    the same ARS_ASR_BACKEND variable and one of them would silently lose."""
    from ars_core import VoiceConfig
    from ars_voice.config import AsrConfig

    assert "asr_backend" in VoiceConfig.model_fields
    assert "backend" not in AsrConfig.model_fields


def test_apple_silicon_defaults_to_the_gpu_backend():
    """faster-whisper int8 measures 8.2 s on this hardware against a 250 ms budget; mlx
    measures 121 ms. The default must not be the one that cannot work."""
    from ars_core.config import default_asr_backend

    with mock.patch("platform.system", return_value="Darwin"), \
         mock.patch("platform.machine", return_value="arm64"):
        assert default_asr_backend() == "mlx-whisper"
    with mock.patch("platform.system", return_value="Linux"), \
         mock.patch("platform.machine", return_value="x86_64"):
        assert default_asr_backend() == "faster-whisper"


def test_no_vendor_sdk_is_imported_by_a_full_mock_pipeline_run():
    script = """
import asyncio, sys
from ars_voice.config import VoicePipelineConfig
from ars_voice.factory import build_pipeline
from ars_voice.asr.mock import ScriptedUtterance
from ars_voice.audio.frames import frames_from_pcm
from ars_voice.audio.synth import keyword_marker_pcm, room_noise_pcm, speech_like_pcm
from ars_voice.eval.wakeword import marker_score
from ars_protocol import Language

# Explicitly the mock path: this asserts what CI runs, and CI has no weights. The local
# .env selects the real backends, and warm-up would load them.
config = VoicePipelineConfig()
config.wakeword.backend = "mock"
config.vad.backend = "energy"
config.core.asr_backend = "mock"
config.tts.backend = "mock"
pipeline = build_pipeline(config, script=[ScriptedUtterance("hello", Language.EN, 0.9)])
pipeline.wakeword._score_fn = marker_score
pcm = room_noise_pcm(300) + keyword_marker_pcm(480) + speech_like_pcm(900) + room_noise_pcm(1100)

async def frames():
    for f in frames_from_pcm(pcm):
        yield f

async def main():
    async for _ in pipeline.run(frames()):
        pass

asyncio.run(main())
vendors = [m for m in ("faster_whisper","mlx","mlx_whisper","piper","openwakeword",
                       "torch","sounddevice","onnxruntime") if m in sys.modules]
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
    config = VoicePipelineConfig()
    config.core.asr_backend = "mock"
    pipeline = build_pipeline(config)
    assert pipeline.endpointer.silence_ms == float(pipeline.config.core.endpoint_silence_ms)
    assert pipeline.synthesizer.engine is pipeline.tts
