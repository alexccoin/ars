"""Playback sinks — where synthesised audio leaves the pipeline.

A sink must be interruptible. `flush()` on barge-in drops queued audio; without it the
user hears half a sentence after they have already started talking over it, which reads
as A.R.S ignoring them.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from ars_protocol import SAMPLE_RATE_HZ, SynthesisChunk
from ars_protocol.audio import BYTES_PER_SAMPLE


class AudioSink(ABC):
    @abstractmethod
    async def write(self, chunk: SynthesisChunk) -> None: ...

    @abstractmethod
    async def flush(self) -> None:
        """Discard anything not yet heard. Called on barge-in."""

    async def close(self) -> None:
        return None


class NullSink(AudioSink):
    """Discards audio, records what it was given. The default in tests and headless runs."""

    def __init__(self) -> None:
        self.chunks: list[SynthesisChunk] = []
        self.flushed = 0

    async def write(self, chunk: SynthesisChunk) -> None:
        self.chunks.append(chunk)

    async def flush(self) -> None:
        self.flushed += 1

    @property
    def total_ms(self) -> float:
        total_bytes = sum(len(c.pcm) for c in self.chunks)
        return total_bytes / BYTES_PER_SAMPLE / SAMPLE_RATE_HZ * 1000.0


class PacedSink(NullSink):
    """Consumes audio at wall-clock speed, like a real speaker.

    Barge-in tests are meaningless against a sink that swallows a 4-second reply in 2 ms:
    there is no 'mid-utterance' to interrupt.
    """

    def __init__(self, *, speed: float = 1.0) -> None:
        super().__init__()
        self._speed = max(speed, 0.001)

    async def write(self, chunk: SynthesisChunk) -> None:
        duration_s = len(chunk.pcm) / BYTES_PER_SAMPLE / SAMPLE_RATE_HZ
        await asyncio.sleep(duration_s / self._speed)
        await super().write(chunk)


class SpeakerSink(AudioSink):
    """sounddevice playback. Lazy import, same reason as MicrophoneSource."""

    def __init__(self, *, device: int | str | None = None) -> None:
        self._device = device
        self._stream = None

    def _ensure_stream(self):  # pragma: no cover - device
        if self._stream is None:
            try:
                import sounddevice as sd
            except ImportError as exc:
                raise RuntimeError(
                    "SpeakerSink needs the 'io' extra: uv pip install -e 'services/voice[io]'"
                ) from exc
            self._stream = sd.RawOutputStream(
                samplerate=SAMPLE_RATE_HZ, channels=1, dtype="int16", device=self._device
            )
            self._stream.start()
        return self._stream

    async def write(self, chunk: SynthesisChunk) -> None:  # pragma: no cover - device
        stream = self._ensure_stream()
        await asyncio.to_thread(stream.write, chunk.pcm)

    async def flush(self) -> None:  # pragma: no cover - device
        if self._stream is not None:
            self._stream.abort()
            self._stream.start()

    async def close(self) -> None:  # pragma: no cover - device
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
