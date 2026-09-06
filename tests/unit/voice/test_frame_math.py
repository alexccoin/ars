"""Frame arithmetic must agree with `packages/protocol`. A second definition of the sample
rate or the frame size anywhere in the voice service is the bug this file exists to catch."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from ars_protocol import (
    BYTES_PER_FRAME,
    FRAME_MS,
    SAMPLE_RATE_HZ,
    SAMPLES_PER_FRAME,
    AudioFrame,
)
from ars_protocol.audio import BYTES_PER_SAMPLE
from ars_voice.audio.frames import (
    PreRollBuffer,
    bytes_for_ms,
    frame_duration_ms,
    frames_for_ms,
    frames_from_pcm,
    ms_for_frames,
    pcm_from_frames,
    reseq,
    validate_frame,
)

SOURCE = Path(__file__).resolve().parents[3] / "services" / "voice" / "src"


def test_protocol_constants_are_self_consistent():
    assert SAMPLES_PER_FRAME == SAMPLE_RATE_HZ * FRAME_MS // 1000
    assert BYTES_PER_FRAME == SAMPLES_PER_FRAME * BYTES_PER_SAMPLE


def test_frames_for_ms_rounds_up_and_round_trips():
    assert frames_for_ms(0) == 0
    assert frames_for_ms(FRAME_MS) == 1
    assert frames_for_ms(FRAME_MS + 1) == 2
    assert frames_for_ms(500) == 500 // FRAME_MS
    assert ms_for_frames(frames_for_ms(500)) == 500


def test_bytes_for_ms_matches_the_protocol_frame_size():
    assert bytes_for_ms(FRAME_MS) == BYTES_PER_FRAME
    assert bytes_for_ms(1000) == SAMPLE_RATE_HZ * BYTES_PER_SAMPLE


def test_frames_from_pcm_produces_whole_frames_and_monotonic_seq():
    pcm = b"\x00" * (BYTES_PER_FRAME * 5 + 7)
    produced = frames_from_pcm(pcm)
    assert len(produced) == 5  # the 7-byte runt is dropped
    assert [f.seq for f in produced] == [0, 1, 2, 3, 4]
    assert all(len(f.pcm) == BYTES_PER_FRAME for f in produced)
    assert frame_duration_ms(produced[0]) == pytest.approx(FRAME_MS)
    assert pcm_from_frames(produced) == pcm[: BYTES_PER_FRAME * 5]


def test_validate_frame_rejects_a_half_sample():
    with pytest.raises(ValueError, match="whole number"):
        validate_frame(AudioFrame(seq=0, pcm=b"\x00" * 3))
    with pytest.raises(ValueError, match="BYTES_PER_FRAME"):
        validate_frame(AudioFrame(seq=0, pcm=b"\x00" * (BYTES_PER_FRAME + 2)))


def test_reseq_keeps_the_spliced_stream_monotonic():
    pre_roll = frames_from_pcm(b"\x11" * BYTES_PER_FRAME * 3, start_seq=900)
    live = frames_from_pcm(b"\x22" * BYTES_PER_FRAME * 2, start_seq=1000)
    merged = reseq(pre_roll, start=0) + reseq(live, start=3)
    assert [f.seq for f in merged] == [0, 1, 2, 3, 4]
    assert pcm_from_frames(merged) == b"\x11" * BYTES_PER_FRAME * 3 + b"\x22" * BYTES_PER_FRAME * 2


def test_pre_roll_capacity_is_derived_from_the_protocol_frame_size():
    buffer = PreRollBuffer(500)
    assert buffer.capacity_frames == 500 // FRAME_MS
    assert buffer.capacity_ms == 500


def test_no_sample_rate_literal_anywhere_in_the_service():
    """`ars_protocol.audio` says a hardcoded rate is a defect. This asserts it."""
    pattern = re.compile(r"\b16[_ ]?000\b")
    offenders = [
        f"{path.relative_to(SOURCE)}:{number}: {line.strip()}"
        for path in sorted(SOURCE.rglob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if pattern.search(line) and "SAMPLE_RATE_HZ" not in line and "ars_protocol" not in line
    ]
    assert offenders == [], "hardcoded sample rate: " + "; ".join(offenders)
