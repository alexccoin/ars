r"""Frame arithmetic and PCM helpers.

Every constant in here comes from `ars_protocol.audio`. There is no sample-rate literal
anywhere in `services/voice`: ``grep -rnE "16[_ ]?000" services/voice/src`` matches nothing
outside this docstring. A hardcoded rate is a defect, per ars_protocol.audio.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Iterator, Sequence

import numpy as np
from ars_protocol import (
    BYTES_PER_FRAME,
    FRAME_MS,
    SAMPLE_RATE_HZ,
    SAMPLES_PER_FRAME,
    AudioFrame,
)
from ars_protocol.audio import BYTES_PER_SAMPLE

INT16_FULL_SCALE = 32768.0
SILENCE_DBFS = -120.0
"""Floor reported for digital silence, so callers never see -inf in a log line."""

__all__ = [
    "INT16_FULL_SCALE",
    "SILENCE_DBFS",
    "PreRollBuffer",
    "bytes_for_ms",
    "frame_duration_ms",
    "frames_for_ms",
    "frames_from_pcm",
    "ms_for_frames",
    "pcm_from_frames",
    "pcm_to_float32",
    "rms_dbfs",
    "silence_pcm",
    "validate_frame",
]


def frames_for_ms(ms: float) -> int:
    """Number of whole protocol frames needed to cover `ms` (rounded up)."""
    if ms <= 0:
        return 0
    return math.ceil(ms / FRAME_MS)


def ms_for_frames(count: int) -> int:
    return count * FRAME_MS


def bytes_for_ms(ms: float) -> int:
    """Byte count for `ms` of audio, snapped down to a whole number of samples."""
    return int(ms * SAMPLE_RATE_HZ / 1000) * BYTES_PER_SAMPLE


def frame_duration_ms(frame: AudioFrame) -> float:
    """A frame is normally FRAME_MS; the last frame of a file may be short."""
    return len(frame.pcm) / BYTES_PER_SAMPLE / SAMPLE_RATE_HZ * 1000.0


def validate_frame(frame: AudioFrame) -> AudioFrame:
    """Reject frames that are not a whole number of samples.

    A half-sample frame means someone sliced a byte stream on the wrong boundary, which
    shows up later as a click in TTS or a garbled word in ASR and is very hard to trace
    back here.
    """
    if len(frame.pcm) % BYTES_PER_SAMPLE:
        raise ValueError(
            f"frame {frame.seq}: {len(frame.pcm)} bytes is not a whole number of "
            f"{BYTES_PER_SAMPLE}-byte samples"
        )
    if len(frame.pcm) > BYTES_PER_FRAME:
        raise ValueError(
            f"frame {frame.seq}: {len(frame.pcm)} bytes exceeds BYTES_PER_FRAME={BYTES_PER_FRAME}"
        )
    return frame


def silence_pcm(ms: float) -> bytes:
    return b"\x00" * bytes_for_ms(ms)


def pcm_to_float32(pcm: bytes) -> np.ndarray:
    """int16 PCM -> float32 in [-1, 1). Every model we integrate wants this shape."""
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / INT16_FULL_SCALE


def float32_to_pcm(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0 - 1.0 / INT16_FULL_SCALE)
    return (clipped * INT16_FULL_SCALE).astype("<i2").tobytes()


def rms_dbfs(pcm: bytes) -> float:
    """Frame energy in dBFS. The energy VAD's only input."""
    samples = pcm_to_float32(pcm)
    if samples.size == 0:
        return SILENCE_DBFS
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    if rms <= 0.0:
        return SILENCE_DBFS
    return max(SILENCE_DBFS, 20.0 * math.log10(rms))


def frames_from_pcm(
    pcm: bytes,
    *,
    start_seq: int = 0,
    captured_at_ms: int | None = None,
    drop_partial: bool = True,
) -> list[AudioFrame]:
    """Slice a PCM buffer into protocol frames.

    `drop_partial` discards a trailing fragment shorter than a full frame: ASR models are
    fed fixed-size frames and a runt frame at the end of every utterance is a needless
    special case downstream.
    """
    out: list[AudioFrame] = []
    seq = start_seq
    for offset in range(0, len(pcm), BYTES_PER_FRAME):
        chunk = pcm[offset : offset + BYTES_PER_FRAME]
        if len(chunk) < BYTES_PER_FRAME and drop_partial:
            break
        kwargs = (
            {} if captured_at_ms is None
            else {"captured_at_ms": captured_at_ms + seq * FRAME_MS}
        )
        out.append(AudioFrame(seq=seq, pcm=chunk, **kwargs))
        seq += 1
    return out


def pcm_from_frames(frames: Iterable[AudioFrame]) -> bytes:
    return b"".join(f.pcm for f in frames)


def total_duration_ms(frames: Sequence[AudioFrame]) -> float:
    return sum(frame_duration_ms(f) for f in frames)


class PreRollBuffer:
    """Fixed-capacity ring of recent frames.

    This is the reason the first syllable after the wakeword survives. The wakeword only
    fires *after* it has heard the whole keyword, by which time the user is already saying
    the next word; without a pre-roll, ASR receives "…mail to Andrei" and the verb is gone.

    Capacity is measured in milliseconds and converted with the protocol frame size, so a
    change to FRAME_MS does not silently halve the retained audio.
    """

    __slots__ = ("_capacity_frames", "_capacity_ms", "_frames")

    def __init__(self, capacity_ms: float) -> None:
        if capacity_ms < 0:
            raise ValueError("capacity_ms must be >= 0")
        self._capacity_ms = float(capacity_ms)
        self._capacity_frames = frames_for_ms(capacity_ms)
        self._frames: deque[AudioFrame] = deque(maxlen=max(self._capacity_frames, 1))

    @property
    def capacity_ms(self) -> float:
        return self._capacity_ms

    @property
    def capacity_frames(self) -> int:
        return self._capacity_frames

    @property
    def duration_ms(self) -> float:
        return total_duration_ms(tuple(self._frames))

    def push(self, frame: AudioFrame) -> None:
        if self._capacity_frames == 0:
            return
        self._frames.append(validate_frame(frame))

    def extend(self, frames: Iterable[AudioFrame]) -> None:
        for frame in frames:
            self.push(frame)

    def snapshot(self, ms: float | None = None) -> tuple[AudioFrame, ...]:
        """The most recent `ms` of audio, oldest first.

        Returns whole frames only, and never more than the buffer holds — asking for 500 ms
        of pre-roll 200 ms after the stream opened returns 200 ms, not padding. Callers that
        need to know use `duration_ms`.
        """
        if ms is None:
            return tuple(self._frames)
        wanted = min(frames_for_ms(ms), len(self._frames))
        if wanted <= 0:
            return ()
        return tuple(self._frames)[-wanted:]

    def clear(self) -> None:
        self._frames.clear()

    def __len__(self) -> int:
        return len(self._frames)

    def __iter__(self) -> Iterator[AudioFrame]:
        return iter(tuple(self._frames))


def reseq(frames: Iterable[AudioFrame], start: int = 0) -> list[AudioFrame]:
    """Renumber a frame run so `seq` stays monotonic after pre-roll is spliced in front.

    A gap in `seq` is how the rest of the system detects dropped audio; splicing the ring
    buffer onto the live stream would otherwise look exactly like a drop.
    """
    return [
        AudioFrame(seq=start + i, captured_at_ms=f.captured_at_ms, pcm=f.pcm)
        for i, f in enumerate(frames)
    ]


assert SAMPLES_PER_FRAME * BYTES_PER_SAMPLE == BYTES_PER_FRAME  # protocol self-check
