"""The latency budget, asserted.

CLAUDE.md: "Latency is a test. The budget in the architecture doc is asserted in tests/load."
This file asserts the four rows `services/voice` owns, as amended by
docs/adr/0001-endpointing-latency-budget.md (ENDPOINTING 750 ms, TOTAL 1400 ms).

Audio is replayed at wall clock. That is the whole point — the endpointing row is a duration
the user experiences, and replaying frames as fast as the loop takes them reports ~1 ms and
proves nothing.

Two suites:

* the **mock** path bounds the plumbing — scheduling, queueing, the state machine, the
  endpointing window — and runs anywhere;
* the **real** path runs the configured backends over `data/fixtures/spoken` and is skipped
  when the weights are absent. It is the one that decides whether A.R.S ships.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from ars_voice.config import VoicePipelineConfig
from ars_voice.eval.report import run_real_turns, run_turns
from ars_voice.fixtures import DEFAULT_DIR
from ars_voice.metrics import BUDGET_P95_MS, VOICE_OWNED, Stage

TURNS = 3
REAL_TURNS = 4

pytestmark = pytest.mark.skipif(
    not (DEFAULT_DIR / "wakeword_positive" / "manifest.json").is_file(),
    reason="fixtures not generated — run `ars-voice-fixtures all`",
)


def _real_available() -> bool:
    config = VoicePipelineConfig()
    if not (DEFAULT_DIR / "spoken" / "manifest.json").is_file():
        return False
    required = {
        "mlx-whisper": "mlx_whisper", "faster-whisper": "faster_whisper",
        "piper": "piper", "openwakeword": "openwakeword", "silero": "silero_vad",
    }
    for name in (config.core.asr_backend, config.tts.backend,
                 config.wakeword.backend, config.vad.backend):
        module = required.get(name)
        if module and importlib.util.find_spec(module) is None:
            return False
    return Path("models").is_dir()


needs_real = pytest.mark.skipif(not _real_available(), reason="real backends or weights missing")


@pytest.fixture(scope="module")
async def recorder():
    rec, _states = await run_turns(TURNS, speed=1.0, root=DEFAULT_DIR)
    return rec


@pytest.mark.parametrize(
    "stage", [Stage.WAKEWORD_DETECTION, Stage.ENDPOINTING, Stage.ASR_FINAL, Stage.TTS_FIRST_AUDIO]
)
async def test_voice_owned_rows_are_within_budget_on_the_mock_path(recorder, stage: Stage):
    stats = recorder.stats(stage)
    assert stats.count == TURNS
    assert stats.within_budget is True, (
        f"{stage.value}: p95 {stats.p95_ms:.0f} ms > {BUDGET_P95_MS[stage]:.0f} ms budget"
    )


async def test_no_budgeted_row_goes_unmeasured(recorder):
    """"Nothing was measured" and "everything passed" look identical in a report that only
    lists breaches. This is the difference."""
    assert recorder.unmeasured() == []


async def test_endpointing_matches_the_configured_silence_window(recorder):
    """What must not drift is the relationship between the setting and the observed wait.
    If this fails, something other than `endpoint_silence_ms` is adding a delay — that is how
    a stacked VAD hangover cost 200 ms a turn until it was measured."""
    config = VoicePipelineConfig()
    stats = recorder.stats(Stage.ENDPOINTING)
    assert stats.p95_ms == pytest.approx(config.core.endpoint_silence_ms, abs=120)


async def test_total_is_within_budget_on_the_mock_path(recorder):
    """With no model inference, TOTAL has to fit comfortably. If this fails on mocks the
    pipeline itself has grown a stall and no model change will save it."""
    assert recorder.stats(Stage.TOTAL).p95_ms < BUDGET_P95_MS[Stage.TOTAL]


# --------------------------------------------------------------------------- real engines

@pytest.fixture(scope="module")
async def real_run():
    return await run_real_turns(REAL_TURNS, speed=1.0, root=DEFAULT_DIR)


@needs_real
async def test_real_engines_transcribe_both_languages_correctly(real_run):
    """A fast wrong answer is not a pass. The ASR backend was changed for latency; this is
    the assertion that the change did not cost correctness."""
    _recorder, outcomes, _warm = real_run
    assert {o["expected_language"] for o in outcomes} == {"en", "ro"}, "both languages needed"
    for outcome in outcomes:
        assert outcome["language"] == outcome["expected_language"], outcome
        assert outcome["confidence"] > 0.9, outcome
        assert outcome["text"].strip(), outcome


@needs_real
async def test_real_total_is_within_budget(real_run):
    """The number that matters: wakeword to first audio, excluding the user's own speech."""
    recorder, _outcomes, _warm = real_run
    stats = recorder.stats(Stage.TOTAL)
    assert stats.count == REAL_TURNS
    assert stats.p95_ms < BUDGET_P95_MS[Stage.TOTAL], (
        f"real p95 total {stats.p95_ms:.0f} ms > {BUDGET_P95_MS[Stage.TOTAL]:.0f} ms"
    )


@needs_real
@pytest.mark.parametrize("stage", VOICE_OWNED)
async def test_real_voice_owned_rows(real_run, stage: Stage):
    """Per-row, with a tolerance: p95 over four turns is the max of four samples, so a
    single scheduling hiccup moves it. The tolerance is there to catch a *regression*, not to
    excuse one — the honest per-run numbers are in `ars-voice-latency --engines real`."""
    recorder, _outcomes, _warm = real_run
    stats = recorder.stats(stage)
    assert stats.count == REAL_TURNS
    budget = BUDGET_P95_MS[stage]
    assert stats.p50_ms <= budget, (
        f"{stage.value}: p50 {stats.p50_ms:.0f} ms over the {budget:.0f} ms budget"
    )
    assert stats.p95_ms <= budget * 1.5, (
        f"{stage.value}: p95 {stats.p95_ms:.0f} ms is far over the {budget:.0f} ms budget"
    )


@needs_real
async def test_warm_up_happens_before_the_clock_starts(real_run):
    """Cold model loads are seconds. They are paid at startup by `VoicePipeline.warm_up`, and
    this asserts they were actually paid — a zero here means warm-up silently did nothing and
    the first turn is carrying the load."""
    _recorder, _outcomes, warm = real_run
    assert set(warm) == {"wakeword", "vad", "asr", "tts"}
    assert warm["tts"] > 100, "TTS warm-up did no work; both voices must be preloaded"
    assert warm["asr"] > 100, "ASR warm-up did no work"
