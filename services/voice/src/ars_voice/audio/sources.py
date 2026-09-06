"""Frame sources — where audio enters the pipeline.

All of them yield `AudioFrame` at the protocol's rate and frame size. Anything that
resamples does it here, at capture, never deeper in.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import wave
from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path

import numpy as np
from ars_protocol import (
    BYTES_PER_FRAME,
    FRAME_MS,
    SAMPLE_RATE_HZ,
    SAMPLES_PER_FRAME,
    AudioFrame,
    now_ms,
)

from .frames import frames_from_pcm


async def frames_from_iterable(
    frames: Iterable[AudioFrame], *, realtime: bool = False, speed: float = 1.0
) -> AsyncIterator[AudioFrame]:
    """Replay a fixed frame list.

    `realtime=True` paces at wall-clock frame duration, which is what makes a latency
    measurement mean anything: without pacing you measure how fast Python can loop, not how
    long the user waits.

    Paced against an absolute schedule, not by sleeping FRAME_MS between frames.
    `asyncio.sleep` overshoots by a fraction of a millisecond, and sleeping per frame
    accumulates that: over the 35 frames of a 700 ms silence window it added ~35 ms of drift
    under load and showed up as endpointing latency that was not there. A real capture device
    delivers frames on a hardware clock and does not drift, so a harness that does is
    measuring itself.
    """
    if not realtime:
        for frame in frames:
            await asyncio.sleep(0)
            yield frame
        return

    interval = (FRAME_MS / 1000.0) / max(speed, 0.001)
    start = time.perf_counter()
    for index, frame in enumerate(frames):
        deadline = start + (index + 1) * interval
        remaining = deadline - time.perf_counter()
        if remaining > 0:
            await asyncio.sleep(remaining)
        else:
            # Behind schedule: yield to the loop but do not sleep the debt away, or the
            # stream silently slows down and every downstream duration inherits the lag.
            await asyncio.sleep(0)
        yield frame


def read_wav(path: Path | str) -> bytes:
    """Read a mono 16-bit WAV at the protocol rate. Deliberately strict.

    No ffmpeg, no resampling library: a fixture at the wrong rate is a broken fixture and
    silently resampling it would make every number measured against it a lie.
    """
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1:
            raise ValueError(f"{path}: expected mono, got {wav.getnchannels()} channels")
        if wav.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit PCM, got {wav.getsampwidth() * 8}-bit")
        if wav.getframerate() != SAMPLE_RATE_HZ:
            raise ValueError(
                f"{path}: expected {SAMPLE_RATE_HZ} Hz (ars_protocol.SAMPLE_RATE_HZ), "
                f"got {wav.getframerate()} Hz — resample at capture, not here"
            )
        return wav.readframes(wav.getnframes())


def write_wav(path: Path | str, pcm: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE_HZ)
        wav.writeframes(pcm)


async def frames_from_wav(
    path: Path | str, *, realtime: bool = False, start_seq: int = 0
) -> AsyncIterator[AudioFrame]:
    frames = frames_from_pcm(read_wav(path), start_seq=start_seq, captured_at_ms=now_ms())
    async for frame in frames_from_iterable(frames, realtime=realtime):
        yield frame


class QueueFrameSource:
    """Push-driven source: the transport (WebSocket) pushes, the pipeline pulls.

    Bounded on purpose. If the consumer falls behind, dropping the oldest frame and
    counting it is honest; an unbounded queue turns a CPU stall into a growing latency
    debt that never repays and looks like the model got slow.
    """

    def __init__(self, maxsize: int = 200) -> None:
        self._queue: asyncio.Queue[AudioFrame | None] = asyncio.Queue(maxsize=maxsize)
        self.dropped_frames = 0

    async def push(self, frame: AudioFrame) -> None:
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self.dropped_frames += 1
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(frame)

    async def close(self) -> None:
        await self._queue.put(None)

    async def frames(self) -> AsyncIterator[AudioFrame]:
        while True:
            frame = await self._queue.get()
            if frame is None:
                return
            yield frame


class MicrophoneSource:
    """Real capture via sounddevice. Imported lazily — `services/voice` must import on a
    machine with no audio device at all, which is exactly what CI is."""

    def __init__(self, *, device: int | str | None = None, blocksize_frames: int = 1) -> None:
        self._device = device
        self._blocksize = SAMPLES_PER_FRAME * blocksize_frames

    async def frames(self) -> AsyncIterator[AudioFrame]:
        try:
            import sounddevice as sd
        except ImportError as exc:  # pragma: no cover - depends on host audio stack
            raise RuntimeError(
                "MicrophoneSource needs the 'io' extra: uv pip install -e 'services/voice[io]'"
            ) from exc

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)

        def callback(indata, _frames, _time, status) -> None:  # pragma: no cover - device
            if status:
                pass  # over/underflow is reported by the pipeline's frame-gap detector
            loop.call_soon_threadsafe(_offer, bytes(indata))

        def _offer(pcm: bytes) -> None:  # pragma: no cover - device
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(pcm)

        seq = 0
        stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE_HZ,
            blocksize=self._blocksize,
            device=self._device,
            channels=1,
            dtype="int16",
            callback=callback,
        )
        with stream:  # pragma: no cover - device
            while True:
                pcm = await queue.get()
                for offset in range(0, len(pcm), BYTES_PER_FRAME):
                    chunk = pcm[offset : offset + BYTES_PER_FRAME]
                    if len(chunk) == BYTES_PER_FRAME:
                        yield AudioFrame(seq=seq, pcm=chunk)
                        seq += 1


def resample_to_protocol_rate(samples: np.ndarray, source_rate_hz: int) -> np.ndarray:
    """Linear resample to SAMPLE_RATE_HZ. Capture-side only.

    Linear interpolation is not good enough for an ASR front end on a real device — it
    aliases. It is here so a 48 kHz mock/desktop capture can be wired up today; a device
    backend should ask its audio stack for SAMPLE_RATE_HZ directly.
    """
    if source_rate_hz == SAMPLE_RATE_HZ or samples.size == 0:
        return samples
    duration = samples.size / source_rate_hz
    target_n = int(duration * SAMPLE_RATE_HZ)
    src_x = np.arange(samples.size, dtype=np.float64) / source_rate_hz
    dst_x = np.arange(target_n, dtype=np.float64) / SAMPLE_RATE_HZ
    return np.interp(dst_x, src_x, samples).astype(samples.dtype)


def concat_sources(*sequences: Sequence[AudioFrame]) -> list[AudioFrame]:
    out: list[AudioFrame] = []
    seq = 0
    for sequence in sequences:
        for frame in sequence:
            out.append(AudioFrame(seq=seq, captured_at_ms=frame.captured_at_ms, pcm=frame.pcm))
            seq += 1
    return out
