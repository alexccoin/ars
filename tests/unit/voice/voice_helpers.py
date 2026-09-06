"""Deterministic audio builders for the voice unit tests.

Everything here runs on mock engines: no weights, no microphone, no network. That is a
requirement, not a convenience — CI has none of those, and a test suite that needs them
stops being run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ars_protocol import AudioFrame
from ars_voice.audio.frames import frames_from_pcm
from ars_voice.audio.synth import keyword_marker_pcm, mix, room_noise_pcm, speech_like_pcm


def noisy(pcm: bytes, *, level_dbfs: float = -62.0, seed: int = 0) -> bytes:
    ms = len(pcm) / 32
    return mix(pcm, room_noise_pcm(ms, level_dbfs=level_dbfs, seed=seed))


def silence(ms: float, *, seed: int = 0) -> bytes:
    return room_noise_pcm(ms, level_dbfs=-62.0, seed=seed)


def speech(ms: float, *, seed: int = 1, dbfs: float = -22.0) -> bytes:
    return noisy(speech_like_pcm(ms, amplitude_dbfs=dbfs, seed=seed), seed=seed + 7)


def keyword(ms: float = 480.0, *, seed: int = 3) -> bytes:
    return noisy(keyword_marker_pcm(ms, seed=seed), seed=seed + 11)


def frames(pcm: bytes, start_seq: int = 0) -> list[AudioFrame]:
    return frames_from_pcm(pcm, start_seq=start_seq)


async def stream(items: list[AudioFrame], *, delay: float = 0.0) -> AsyncIterator[AudioFrame]:
    import asyncio

    for frame in items:
        await asyncio.sleep(delay)
        yield frame
