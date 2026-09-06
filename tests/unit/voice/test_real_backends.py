"""The real engines, against real synthesised speech.

These run only when the weights are on disk and the backend package imports; CI has neither,
and the mock path stays the one that must always be green. They are here because everything
that actually broke in this service broke on contact with a real model: silero's loader
signature, openWakeWord's `_v0.1` filenames, the wake phrase bleeding into the transcript,
and a language-detection path that silently fell back to English.

They are cheap: the model load is shared across the module and a decode is ~130 ms.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest
from ars_protocol import Language, SynthesisRequest
from ars_voice.audio.frames import frames_from_pcm
from ars_voice.audio.sources import frames_from_iterable, read_wav
from ars_voice.config import VoicePipelineConfig
from ars_voice.metrics import BUDGET_P95_MS, LatencyRecorder, Stage

ROOT = Path(__file__).resolve().parents[3]
SPOKEN = ROOT / "data" / "fixtures" / "spoken"


def _has(module: str, *paths: Path) -> bool:
    return importlib.util.find_spec(module) is not None and all(p.exists() for p in paths)


needs_mlx = pytest.mark.skipif(
    not _has("mlx_whisper", SPOKEN / "manifest.json"),
    reason="mlx-whisper or data/fixtures/spoken missing",
)
needs_piper = pytest.mark.skipif(
    not _has("piper", ROOT / "models" / "tts" / "en_US-amy-medium.onnx"),
    reason="piper voices missing",
)
needs_oww = pytest.mark.skipif(
    not _has("openwakeword", ROOT / "models" / "wakeword", SPOKEN / "manifest.json"),
    reason="openwakeword models or spoken fixtures missing",
)
needs_silero = pytest.mark.skipif(
    not _has("silero_vad", ROOT / "models" / "vad" / "silero_vad.onnx", SPOKEN / "manifest.json"),
    reason="silero weights or spoken fixtures missing",
)


def spoken(name: str) -> bytes:
    return read_wav(SPOKEN / f"{name}.wav")


async def stream(pcm: bytes, *, speed: float | None = None):
    """Feed frames. `speed` paces them against the wall clock (8.0 = eight times realtime).

    Unpaced is right for measuring the final decode; anything that depends on a partial
    landing needs pacing, because a partial costs ~100 ms and an unpaced 7 s fixture is fed
    in under a millisecond.
    """
    if speed is None:
        for frame in frames_from_pcm(pcm):
            yield frame
        return
    async for frame in frames_from_iterable(frames_from_pcm(pcm), realtime=True, speed=speed):
        yield frame


# --------------------------------------------------------------------------- ASR

@pytest.fixture(scope="module")
def mlx_engine():
    from ars_voice.asr.mlx_whisper_engine import MlxWhisperEngine

    return MlxWhisperEngine()


@needs_mlx
@pytest.mark.parametrize(
    ("fixture", "language", "must_contain"),
    [
        ("en_short_command", Language.EN, "lights"),
        ("ro_short_command", Language.RO, "lumina"),
        ("en_bank_messages", Language.EN, "bank"),
        ("ro_bank_messages", Language.RO, "mesaje"),
    ],
)
async def test_mlx_whisper_transcribes_both_languages(
    mlx_engine, fixture: str, language: Language, must_contain: str
):
    """Per-utterance detection, not per session: EN and RO fixtures go through the same
    engine instance in the same order a bilingual household would produce them."""
    results = [t async for t in mlx_engine.transcribe(stream(spoken(fixture)))]
    finals = [t for t in results if t.is_final]

    assert len(finals) == 1, "an utterance must end with exactly one final"
    final = finals[-1]
    assert final.language is language, f"{fixture}: heard {final.language.value}"
    assert final.language_confidence > 0.9
    assert must_contain.lower() in final.text.lower(), final.text


@needs_mlx
async def test_mlx_whisper_final_decode_is_inside_the_asr_budget(mlx_engine):
    """The row this backend exists for. faster-whisper int8 measures ~8.2 s here."""
    await mlx_engine.load()
    recorder = LatencyRecorder()
    for fixture in ("en_short_command", "ro_short_command", "en_bank_messages"):
        pcm = spoken(fixture)
        start = LatencyRecorder.mark()
        finals = [t async for t in mlx_engine.transcribe(stream(pcm)) if t.is_final]
        recorder.record(Stage.ASR_FINAL, LatencyRecorder.mark() - start)
        assert finals[-1].text
    stats = recorder.stats(Stage.ASR_FINAL)
    # Whole-utterance wall clock, including feeding every frame and the partial decodes —
    # strictly more than the final decode the budget actually covers.
    assert stats.p95_ms < BUDGET_P95_MS[Stage.ASR_FINAL] * 4, stats


@needs_mlx
async def test_mlx_whisper_refuses_to_guess_a_language(mlx_engine):
    """The bug this replaced: an empty language distribution fell through to English and
    transcribed Romanian audio as fluent, confident English."""
    from ars_voice.asr.mlx_whisper_engine import NoLanguageDistribution

    await mlx_engine.load()

    class Empty:
        language_probs = None
        audio_features = None

    original = mlx_engine._decoding.decode
    mlx_engine._decoding.decode = lambda *a, **k: Empty()
    try:
        with pytest.raises(NoLanguageDistribution):
            mlx_engine._detect(object())
    finally:
        mlx_engine._decoding.decode = original


@needs_mlx
async def test_mlx_whisper_emits_partials_before_the_final(mlx_engine):
    await mlx_engine.load()
    results = [
        t async for t in mlx_engine.transcribe(stream(spoken("en_bank_messages"), speed=8.0))
    ]
    assert any(not t.is_final for t in results), "no partials — the UI has no evidence"
    assert results[-1].is_final
    # Partials make no language claim; only the final does.
    assert all(t.language_confidence == 0.0 for t in results if not t.is_final)


@needs_mlx
async def test_partials_are_skipped_while_the_user_is_silent(mlx_engine):
    """Protects the budgeted row: a partial started during trailing silence lands on top of
    the final decode."""
    await mlx_engine.load()
    mlx_engine.set_speech_active(False)
    before = mlx_engine.partials_skipped
    finals = [t async for t in mlx_engine.transcribe(stream(spoken("en_bank_messages")))
              if t.is_final]
    mlx_engine.set_speech_active(True)
    assert mlx_engine.partials_skipped > before
    assert finals and finals[-1].text


# --------------------------------------------------------------------------- TTS

@needs_piper
async def test_piper_warm_up_covers_both_voices_and_meets_the_budget_afterwards():
    """Cold, a Piper voice load is ~355 ms against a 120 ms budget — and it is per voice, so
    a bilingual household would miss the budget twice without this."""
    from ars_voice.tts.piper import PiperTtsEngine

    engine = PiperTtsEngine(model_dir=ROOT / "models" / "tts")
    timings = await engine.warm_up()
    assert set(timings) == {Language.EN, Language.RO}

    for language, text in (
        (Language.EN, "Good morning. I found three new messages."),
        (Language.RO, "Bună dimineața. Am găsit trei mesaje noi."),
    ):
        start = LatencyRecorder.mark()
        first = None
        async for chunk in engine.synthesize(
            SynthesisRequest(text=text, language=language)
        ):
            if chunk.pcm:
                first = LatencyRecorder.mark() - start
                break
        assert first is not None
        assert first < BUDGET_P95_MS[Stage.TTS_FIRST_AUDIO], (
            f"{language.value} warm time-to-first-audio {first:.0f} ms"
        )


@needs_piper
async def test_breaking_out_of_a_real_synthesis_does_not_strand_a_worker_thread():
    """Time-to-first-audio is measured by taking the first chunk and walking away, and the
    streaming synthesiser abandons the rest of a reply on barge-in. Both close the generator
    without calling cancel(). Piper's worker thread used to spin for ever on a full queue in
    that case, and the process would not exit."""
    import threading

    from ars_voice.tts.piper import PiperTtsEngine

    engine = PiperTtsEngine(model_dir=ROOT / "models" / "tts")
    await engine.warm_up([Language.EN])
    before = threading.active_count()
    for _ in range(3):
        async for chunk in engine.synthesize(
            SynthesisRequest(text="Good morning. I found three new messages.", language=Language.EN)
        ):
            if chunk.pcm:
                break
    await asyncio.sleep(0.3)
    assert threading.active_count() <= before + 1, "worker threads leaked"


@needs_piper
async def test_piper_cancel_stops_a_real_synthesis():
    from ars_voice.tts.piper import PiperTtsEngine

    engine = PiperTtsEngine(model_dir=ROOT / "models" / "tts")
    await engine.warm_up([Language.EN])
    long_text = " ".join(["This is a long reply that takes a while to speak aloud."] * 8)
    produced = 0

    async def consume():
        nonlocal produced
        async for _ in engine.synthesize(
            SynthesisRequest(text=long_text, language=Language.EN)
        ):
            produced += 1

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.4)
    at_cancel = produced
    await engine.cancel()
    await asyncio.wait_for(task, timeout=10)
    assert engine.cancellations == 1
    assert at_cancel > 0, "the test cancelled before synthesis had started"
    assert produced - at_cancel <= 4, "piper kept streaming well past the cancel"


# --------------------------------------------------------------------------- wakeword

@needs_oww
async def test_openwakeword_fires_once_on_real_speech_and_keeps_the_pre_roll():
    from ars_voice.wakeword.openwakeword_engine import OpenWakeWordEngine

    config = VoicePipelineConfig()
    engine = OpenWakeWordEngine(
        keyword=config.core.wakeword,
        threshold=config.core.wakeword_threshold,
        model_dir=ROOT / "models" / "wakeword",
        pre_roll_ms=config.wakeword.pre_roll_ms,
    )
    events = [e async for e in engine.detect(stream(spoken("en_short_command")))]
    assert len(events) == 1, f"{len(events)} detections for one spoken keyword"
    assert events[0].score >= config.core.wakeword_threshold
    assert engine.pre_roll_frames(events[0]), "no pre-roll retained"


@needs_oww
def test_a_keyword_with_no_model_fails_with_the_list_of_what_is_there():
    from ars_voice.wakeword.openwakeword_engine import (
        MissingWakewordModel,
        OpenWakeWordEngine,
        available_keywords,
    )

    models = ROOT / "models" / "wakeword"
    assert "hey_jarvis" in available_keywords(models)
    assert "melspectrogram" not in available_keywords(models), "front-end model listed"

    engine = OpenWakeWordEngine(keyword="hey_ars", model_dir=models)
    with pytest.raises(MissingWakewordModel, match="hey_jarvis"):
        engine._resolve_model()


@needs_oww
def test_versioned_filenames_resolve():
    """openWakeWord ships `hey_jarvis_v0.1.onnx`; the config says `hey_jarvis`."""
    from ars_voice.wakeword.openwakeword_engine import OpenWakeWordEngine

    engine = OpenWakeWordEngine(keyword="hey_jarvis", model_dir=ROOT / "models" / "wakeword")
    assert engine._resolve_model().endswith("hey_jarvis_v0.1.onnx")


# --------------------------------------------------------------------------- VAD

@needs_silero
async def test_silero_and_energy_agree_on_where_real_speech_is():
    """They are interchangeable by contract. If they disagree wildly on real audio, the
    no-weights fallback is not a fallback."""
    from ars_voice.vad.energy import EnergyVadEngine
    from ars_voice.vad.silero import SileroVadEngine

    pcm = spoken("ro_short_command")
    spans = {}
    for name, engine in (
        ("silero", SileroVadEngine(model_dir=ROOT / "models" / "vad")),
        ("energy", EnergyVadEngine()),
    ):
        await engine.prepare()
        marks = [i * 20 for i, f in enumerate(frames_from_pcm(pcm)) if engine.step(f).raw_is_speech]
        assert marks, f"{name} found no speech in real speech"
        spans[name] = (marks[0], marks[-1])
    assert abs(spans["silero"][0] - spans["energy"][0]) < 200, spans
    assert abs(spans["silero"][1] - spans["energy"][1]) < 200, spans
