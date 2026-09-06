"""Deterministic synthetic audio.

Used by the mock TTS engine and by the fixture generator. Deterministic on purpose: a
fixture set that changes between runs cannot produce a before/after number, and "it got
better" would be unfalsifiable.

This is not speech. It is speech-*shaped* — a voiced buzz at a plausible F0 with formant-ish
resonances and an amplitude envelope with syllable rate — which is enough to exercise energy
VAD, endpointing and frame plumbing, and is honestly labelled everywhere it is used.
"""

from __future__ import annotations

import numpy as np
from ars_protocol import SAMPLE_RATE_HZ

from .frames import float32_to_pcm

# Rough formant centres, EN male-ish and RO male-ish. Different enough that the two
# fixture languages are not byte-identical, which would hide language plumbing bugs.
_FORMANTS_EN = (620.0, 1_200.0, 2_500.0)
_FORMANTS_RO = (520.0, 1_450.0, 2_400.0)


def speech_like_pcm(
    duration_ms: float,
    *,
    f0_hz: float = 118.0,
    syllable_rate_hz: float = 4.2,
    amplitude_dbfs: float = -22.0,
    formants: tuple[float, ...] = _FORMANTS_EN,
    seed: int = 0,
) -> bytes:
    """Voiced, syllable-modulated buzz at a controlled level."""
    n = int(duration_ms * SAMPLE_RATE_HZ / 1000)
    if n <= 0:
        return b""
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE_HZ

    signal = np.zeros(n, dtype=np.float64)
    for harmonic in range(1, 12):
        signal += np.sin(2 * np.pi * f0_hz * harmonic * t + rng.uniform(0, 2 * np.pi)) / harmonic
    for centre in formants:
        signal += 0.6 * np.sin(2 * np.pi * centre * t + rng.uniform(0, 2 * np.pi))
    signal += 0.05 * rng.standard_normal(n)

    # Syllable envelope: never fully closes, so the "speech" does not read as silence
    # to the VAD in the middle of a word.
    envelope = 0.45 + 0.55 * (0.5 + 0.5 * np.sin(2 * np.pi * syllable_rate_hz * t - np.pi / 2))
    # 10 ms raised-cosine edges: a hard start is a click, and a click is broadband energy
    # that every VAD in existence calls speech.
    edge = min(int(0.010 * SAMPLE_RATE_HZ), n // 2)
    if edge > 0:
        ramp = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, edge))
        envelope[:edge] *= ramp
        envelope[-edge:] *= ramp[::-1]

    signal *= envelope
    peak = float(np.max(np.abs(signal))) or 1.0
    target = 10 ** (amplitude_dbfs / 20.0)
    signal = signal / peak * target * 2.4  # 2.4: peak -> approx RMS at the requested dBFS
    return float32_to_pcm(signal.astype(np.float32))


def room_noise_pcm(duration_ms: float, *, level_dbfs: float = -62.0, seed: int = 0) -> bytes:
    """Low-level pink-ish noise. Real 'silence' has a noise floor; digital zeros do not,
    and an endpointer tuned on digital zeros falls apart in a kitchen."""
    n = int(duration_ms * SAMPLE_RATE_HZ / 1000)
    if n <= 0:
        return b""
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(n)
    # one-pole lowpass ~ pink tilt
    pink = np.empty(n, dtype=np.float64)
    acc = 0.0
    for i in range(n):
        acc = 0.985 * acc + 0.015 * white[i]
        pink[i] = acc
    rms = float(np.sqrt(np.mean(pink**2))) or 1.0
    pink = pink / rms * (10 ** (level_dbfs / 20.0))
    return float32_to_pcm(pink.astype(np.float32))


KEYWORD_MARKER_HZ = 2_000.0
"""Frequency of the synthetic 'keyword' in generated positive fixtures.

A narrowband marker is not a wakeword. It exists so the *evaluation harness* — windowing,
refractory period, pre-roll, FA/hour arithmetic — can be exercised and tested end to end
with a detector that genuinely detects something, on audio that genuinely contains it.
Numbers produced from it describe the harness, never the real model.
"""


def keyword_marker_pcm(
    duration_ms: float, *, amplitude_dbfs: float = -24.0, seed: int = 0
) -> bytes:
    """A modulated 2 kHz burst standing in for the keyword."""
    n = int(duration_ms * SAMPLE_RATE_HZ / 1000)
    if n <= 0:
        return b""
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE_HZ
    tone = np.sin(2 * np.pi * KEYWORD_MARKER_HZ * t)
    tone *= 0.75 + 0.25 * np.sin(2 * np.pi * 6.0 * t)
    tone += 0.02 * rng.standard_normal(n)
    edge = min(int(0.008 * SAMPLE_RATE_HZ), n // 2)
    if edge > 0:
        ramp = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, edge))
        tone[:edge] *= ramp
        tone[-edge:] *= ramp[::-1]
    peak = float(np.max(np.abs(tone))) or 1.0
    tone = tone / peak * (10 ** (amplitude_dbfs / 20.0)) * 1.4
    return float32_to_pcm(tone.astype(np.float32))


def mix(*layers: bytes) -> bytes:
    """Sum equal-length PCM buffers (shorter ones are zero-padded)."""
    arrays = [np.frombuffer(layer, dtype="<i2").astype(np.float64) for layer in layers if layer]
    if not arrays:
        return b""
    length = max(a.size for a in arrays)
    total = np.zeros(length, dtype=np.float64)
    for a in arrays:
        total[: a.size] += a
    total = np.clip(total, -32768, 32767)
    return total.astype("<i2").tobytes()


def formants_for(language: str) -> tuple[float, ...]:
    return _FORMANTS_RO if language == "ro" else _FORMANTS_EN
