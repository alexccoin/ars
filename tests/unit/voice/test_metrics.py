"""Latency instrumentation, and the budget table it reports against.

The table is transcribed from docs/architecture/overview.md. One test parses the document
and asserts the transcription still matches — a stale copy of a budget is worse than no
budget, because it passes."""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest
from ars_voice.metrics import (
    BUDGET_P95_MS,
    STAGE_OWNER,
    VOICE_OWNED,
    LatencyRecorder,
    Stage,
    percentile,
)

DOC = Path(__file__).resolve().parents[3] / "docs" / "architecture" / "overview.md"

# Wakeword is budgeted in prose, not in the table: it happens before the user speaks,
# so it is not on the speech-end -> first-audio path the table sums. See ADR 0001.
DOC_ROW_TO_STAGE = {
    "Endpoint confirmation (700 ms silence + detection)": Stage.ENDPOINTING,
    "ASR final (after endpoint)": Stage.ASR_FINAL,
    "Context assembly + memory recall": Stage.CONTEXT_ASSEMBLY,
    "LLM first token": Stage.LLM_FIRST_TOKEN,
    "TTS time-to-first-audio": Stage.TTS_FIRST_AUDIO,
    "**Total**": Stage.TOTAL,
}


def parse_budget_table() -> dict[Stage, tuple[float, str]]:
    rows: dict[Stage, tuple[float, str]] = {}
    for line in DOC.read_text().splitlines():
        match = re.match(r"^\|\s*(.+?)\s*\|\s*\**(\d+)\s*ms\**\s*\|\s*(.+?)\s*\|$", line)
        if not match:
            continue
        label, budget, owner = match.groups()
        stage = DOC_ROW_TO_STAGE.get(label)
        if stage is not None:
            rows[stage] = (float(budget), owner.strip())
    return rows


def test_the_budget_table_matches_the_architecture_document():
    rows = parse_budget_table()
    assert len(rows) == len(DOC_ROW_TO_STAGE), f"parsed {sorted(s.value for s in rows)}"
    for stage, (budget, owner) in rows.items():
        assert BUDGET_P95_MS[stage] == budget, f"{stage.value}: budget drifted from the doc"
        assert STAGE_OWNER[stage] == owner, f"{stage.value}: owner drifted from the doc"


def test_the_wakeword_budget_is_still_stated_somewhere():
    """It left the table when the table became "speech end -> first audio" (ADR 0001).
    A budget that quietly stops being written down quietly stops being enforced."""
    assert "**150 ms**" in DOC.read_text()
    assert BUDGET_P95_MS[Stage.WAKEWORD_DETECTION] == 150.0


def test_voice_owns_four_of_the_six_rows():
    owned = [s for s, owner in STAGE_OWNER.items() if owner == "voice-engineer"]
    assert set(VOICE_OWNED) <= set(owned)
    assert len(VOICE_OWNED) == 4


def test_the_rows_sum_to_the_total():
    """The table must add up. When the endpointing row was corrected from 400 to 750 ms
    (ADR 0001) the total had to move with it — a table whose rows do not sum to its own
    total is how a budget stops meaning anything."""
    stages = [s for s in DOC_ROW_TO_STAGE.values() if s is not Stage.TOTAL]
    assert sum(BUDGET_P95_MS[s] for s in stages) == BUDGET_P95_MS[Stage.TOTAL]


def test_percentile_is_nearest_rank_not_interpolated():
    """With tens of samples, interpolation reports a number nobody ever measured."""
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 50) == 20.0
    assert percentile(values, 95) == 40.0
    assert percentile(values, 100) == 40.0
    assert math.isnan(percentile([], 95))


def test_recorder_reports_against_budget_and_flags_breaches():
    recorder = LatencyRecorder()
    for value in (100.0, 120.0, 130.0):
        recorder.record(Stage.WAKEWORD_DETECTION, value)
    for value in (600.0, 700.0, 800.0):
        recorder.record(Stage.ENDPOINTING, value)

    assert recorder.stats(Stage.WAKEWORD_DETECTION).within_budget is True
    assert recorder.stats(Stage.ENDPOINTING).within_budget is False
    assert [b.stage for b in recorder.breaches()] == [Stage.ENDPOINTING]
    assert "OVER by" in recorder.format_report()


def test_a_stage_with_no_budget_is_not_reported_as_passing():
    recorder = LatencyRecorder()
    recorder.record(Stage.ASR_FIRST_PARTIAL, 5_000.0)
    assert recorder.stats(Stage.ASR_FIRST_PARTIAL).within_budget is None
    assert recorder.breaches(voice_only=False) == []


def test_total_excludes_the_time_the_user_spends_speaking():
    """Otherwise a long question makes A.R.S look slow, and no amount of optimisation helps."""
    recorder = LatencyRecorder()
    turn = recorder.start_turn("trn_test")
    turn.wake_audio_ms = 1_000.0
    turn.wake_detected_ms = 1_100.0
    turn.speech_end_ms = 6_000.0  # the user talked for five seconds
    turn.endpoint_ms = 6_700.0
    turn.asr_final_ms = 6_900.0
    turn.tts_requested_ms = 7_000.0
    turn.tts_first_audio_ms = 7_100.0
    durations = recorder.commit(turn)
    assert durations[Stage.TOTAL] == pytest.approx(100 + 1_100)
    assert durations[Stage.ENDPOINTING] == pytest.approx(700)
    assert durations[Stage.ASR_FINAL] == pytest.approx(200)


def test_a_turn_is_never_committed_twice():
    recorder = LatencyRecorder()
    turn = recorder.start_turn()
    turn.wake_audio_ms, turn.wake_detected_ms = 0.0, 50.0
    assert recorder.commit(turn)
    assert recorder.commit(turn) == {}
    assert len(recorder.samples(Stage.WAKEWORD_DETECTION)) == 1


def test_stages_that_did_not_happen_are_absent_not_zero():
    """A barged-in turn has no TTS completion. Recording a 0 would flatter every p95."""
    recorder = LatencyRecorder()
    turn = recorder.start_turn()
    turn.wake_audio_ms, turn.wake_detected_ms = 0.0, 40.0
    durations = recorder.commit(turn)
    assert Stage.TTS_FIRST_AUDIO not in durations
    assert Stage.TOTAL not in durations
