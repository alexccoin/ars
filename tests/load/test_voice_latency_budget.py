"""The latency budget, asserted.

CLAUDE.md: "Latency is a test. The budget in the architecture doc is asserted in tests/load."
This file asserts the four rows `services/voice` owns.

Audio is replayed at wall clock. That is the whole point — the endpointing row is a duration
the user experiences, and replaying frames as fast as the loop takes them reports ~1 ms and
proves nothing.

These run on mock engines, so they bound the *pipeline*: plumbing, scheduling, queueing, the
state machine and the endpointing window. Model inference is not included. Re-run after
`scripts/fetch_voice_models.sh` with the real backends selected to get the numbers that
decide whether A.R.S ships.
"""

from __future__ import annotations

import pytest
from ars_voice.config import VoicePipelineConfig
from ars_voice.eval.report import run_turns
from ars_voice.fixtures import DEFAULT_DIR
from ars_voice.metrics import BUDGET_P95_MS, Stage

TURNS = 3

pytestmark = pytest.mark.skipif(
    not (DEFAULT_DIR / "wakeword_positive" / "manifest.json").is_file(),
    reason="fixtures not generated — run `ars-voice-fixtures all`",
)


@pytest.fixture(scope="module")
async def recorder():
    rec, _states = await run_turns(TURNS, speed=1.0, root=DEFAULT_DIR)
    return rec


@pytest.mark.parametrize(
    "stage", [Stage.WAKEWORD_DETECTION, Stage.ASR_FINAL, Stage.TTS_FIRST_AUDIO]
)
async def test_voice_owned_rows_are_within_budget(recorder, stage: Stage):
    stats = recorder.stats(stage)
    assert stats.count == TURNS
    assert stats.within_budget is True, (
        f"{stage.value}: p95 {stats.p95_ms:.0f} ms > {BUDGET_P95_MS[stage]:.0f} ms budget"
    )


async def test_endpointing_matches_the_configured_silence_window(recorder):
    """The row is over budget by construction; what must not drift is the relationship
    between the setting and the observed wait. If this fails, something other than
    `endpoint_silence_ms` is adding a delay."""
    config = VoicePipelineConfig()
    stats = recorder.stats(Stage.ENDPOINTING)
    assert stats.p95_ms == pytest.approx(config.core.endpoint_silence_ms, abs=120)


async def test_endpointing_row_of_the_budget_table(recorder):
    """No longer xfail. The 400 ms budget was wrong, not the implementation: you cannot
    observe 700 ms of silence in 400 ms, and shortening the window truncates real
    utterances. The budget now reflects the physics — see docs/adr/0001."""
    assert recorder.stats(Stage.ENDPOINTING).within_budget is True


async def test_total_is_within_budget_on_the_mock_path(recorder):
    """With no model inference, TOTAL has to fit comfortably. If this ever fails on mocks,
    the pipeline itself has grown a stall and no model change will save it."""
    stats = recorder.stats(Stage.TOTAL)
    assert stats.p95_ms < BUDGET_P95_MS[Stage.TOTAL]
