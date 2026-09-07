"""Streaming character DSP: the robot and alien voices, as effects over any TTS engine.

Why effects and not more models. The whole sci-fi canon of robot voices — Dalek, Cylon,
HAL, vocoder-Vader — is DSP applied to a human recording; the effect *is* the character.
A separate model would cost another ~100 MB resident per character, would have to exist in
three languages (nothing does Romanian), and would be imitating an effect with a neural
net. Measured cost of doing it here instead: 0.08-1.01 ms per 120 ms chunk, i.e. at worst
0.84% of realtime, against a 120 ms time-to-first-audio budget. See research/voices.md §3.

Every effect in this file is:

* **causal** — no lookahead, so it can run on chunk k without waiting for chunk k+1;
* **stateful across chunks** — phase accumulators, filter `zi`, ring buffers and the
  sample-and-hold grid all carry, which is what keeps chunk boundaries silent;
* **chunk-size invariant** — the same audio comes out at 320, 640 or 1920 samples per
  call, so `TtsConfig.chunk_ms` stays a free knob rather than a tuning parameter of the
  effect. `tests/unit/voice/test_character_voices.py` asserts this rather than assuming it.

Two bugs live here permanently and are easy to reintroduce; both were found by the checks
above rather than by listening, and both are commented at the site:

1. `Chorus`'s ring buffer must hold a whole processing block *plus* the longest delay tap.
   Sized to the delay alone, the write of block k overwrites samples the taps of block k
   still have to read.
2. `Vocoder`'s fricative noise needs ONE generator for the life of the stream. Re-seeding
   per chunk repeats the same noise every chunk, which at 120 ms chunks is an 8.33 Hz buzz.

Reverb is deliberately absent. It is the one effect that needs a tail after the last input
sample, which would mean emitting audio after Piper is done — and that fights `cancel()`
and barge-in for a small aesthetic gain.

No vendor SDK here and no model weights: this is numpy and scipy over int16 PCM, so it is
testable without any of models/tts on disk.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
from ars_protocol import SAMPLE_RATE_HZ
from scipy import signal

MAX_BLOCK = 1920
"""Longest slice any effect processes at once (120 ms at 16 kHz).

`Chorus` is the reason this exists: its ring buffer is sized once, at construction, from
this bound plus the longest delay tap. Callers may hand in any chunk size — a short final
chunk, or a `chunk_ms` someone retunes — and `CharacterChain` slices it down to this before
the effects see it. That is what makes chunk-size invariance a property of the code rather
than of the caller's configuration.
"""


class Effect:
    """One streaming, causal, stateful stage. `__call__` takes and returns float32 in
    [-1, 1] and may be called with any length up to `MAX_BLOCK`."""

    def __call__(self, x: np.ndarray) -> np.ndarray:  # pragma: no cover - interface
        raise NotImplementedError


class RingMod(Effect):
    """`y = x·((1-mix) + mix·cos(2πft))`. The cheapest and highest-value robot effect.

    Below the pitch range (**30 Hz, mix 1.0**) it reads as a buzz welded onto the voice —
    the Dalek. Around 50 Hz it is a cleaner "computer". Above ~150 Hz the sidebands at
    f₀±f stop being harmonically related to the voice and it stops sounding mechanical and
    starts sounding *alien*; 230-410 Hz is the alien register.

    The phase accumulator persists across chunks. Reset it per chunk and every chunk
    boundary starts the cosine at 1.0 again, which is a click at 1/chunk_ms — 8.33 Hz at
    the default 120 ms.
    """

    def __init__(self, freq: float = 50.0, mix: float = 1.0, sr: int = SAMPLE_RATE_HZ) -> None:
        self.w = 2 * np.pi * freq / sr
        self.mix = mix
        self.phase = 0.0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if not n:
            return x
        phase = self.phase + self.w * np.arange(n, dtype=np.float64)
        self.phase = float((self.phase + self.w * n) % (2 * np.pi))
        carrier = (1.0 - self.mix) + self.mix * np.cos(phase)
        return (x * carrier).astype(np.float32)


class BitCrush(Effect):
    """Quantise to `bits` and sample-and-hold every `hold` samples.

    This is what makes a ring-modulated voice sound *digital* rather than merely distorted:
    7-8 bits and a hold of 2-3 puts the effective rate at 5.3-8 kHz. The hold grid is
    anchored to a running sample offset, not to the start of the chunk, or the grid jitters
    with the chunk size and the effect stops being chunk-invariant.
    """

    def __init__(self, bits: int = 8, hold: int = 2) -> None:
        self.q = 2.0 ** (bits - 1)
        self.hold = max(1, hold)
        self.offset = 0
        self.last = 0.0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if not n:
            return x
        y = np.round(x * self.q) / self.q
        if self.hold > 1:
            index = (np.arange(n) + self.offset) // self.hold * self.hold - self.offset
            before = index < 0  # held over from the previous block
            out = np.empty_like(y)
            out[~before] = y[index[~before]]
            out[before] = self.last
            self.offset = (self.offset + n) % self.hold
            if index[-1] >= 0:
                self.last = float(y[index[-1]])
            y = out
        return y.astype(np.float32)


class Comb(Effect):
    """Feedback comb, `y[n] = x[n] + fb·y[n-d]`: a resonance at sr/d that reads as a
    metallic throat. 3-6 ms with feedback 0.55-0.7 puts it at 170-330 Hz.

    Written as an `lfilter` with `zi` carried rather than a per-sample loop. The loop
    version was the first one and cost 3x more for bit-identical output.
    """

    def __init__(
        self, delay_ms: float = 6.0, fb: float = 0.6, mix: float = 0.5, sr: int = SAMPLE_RATE_HZ
    ) -> None:
        self.d = max(1, int(delay_ms * sr / 1000))
        self.a = np.zeros(self.d + 1)
        self.a[0] = 1.0
        self.a[self.d] = -fb
        self.b = np.array([1.0])
        self.zi = np.zeros(self.d)
        self.mix = mix

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if not len(x):
            return x
        out, self.zi = signal.lfilter(self.b, self.a, x, zi=self.zi)
        return ((1 - self.mix) * x + self.mix * out).astype(np.float32)


class Chorus(Effect):
    """LFO-modulated delay taps: several detuned copies of the voice at once.

    Deep and slow (0.9-1.7 ms at 0.7-1.7 Hz) gives the hive/collective read that makes the
    alien presets alien. Shallow and fast (0.13-0.31 ms at 2.6-4.1 Hz) gives ordinary
    chorus, which is a *nicer* voice, not a stranger one.
    """

    def __init__(
        self,
        taps: Sequence[tuple[float, float, float]] = (
            (14.0, 0.31, 2.6),
            (19.0, 0.19, 3.4),
            (25.0, 0.13, 4.1),
        ),
        mix: float = 0.6,
        sr: int = SAMPLE_RATE_HZ,
        max_block: int = MAX_BLOCK,
    ) -> None:
        # BUG CLASS, do not "simplify": the ring buffer must hold one whole processing
        # block PLUS the longest delay. Sized to the delay alone, the write of block k
        # overwrites samples the taps of block k have not read yet, and the effect quietly
        # stops being chunk-size invariant — it still sounds plausible, which is why this
        # is a comment and a test rather than a memory.
        longest_ms = max(base + depth for base, depth, _ in taps)
        self.size = int(max_block + longest_ms * sr / 1000) + 4
        self.buffer = np.zeros(self.size, np.float32)
        self.write = 0
        self.taps = tuple(taps)
        self.mix = mix
        self.sr = sr
        self.t = 0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if not n:
            return x
        positions = np.arange(n)
        self.buffer[(self.write + positions) % self.size] = x
        clock = self.t + positions
        acc = np.zeros(n, np.float32)
        for base_ms, depth_ms, rate_hz in self.taps:
            delay = (
                base_ms + depth_ms * np.sin(2 * np.pi * rate_hz * clock / self.sr)
            ) * self.sr / 1000.0
            read = (self.write + positions - delay) % self.size
            low = read.astype(np.int32)
            frac = (read - low).astype(np.float32)
            acc += (1 - frac) * self.buffer[low] + frac * self.buffer[(low + 1) % self.size]
        acc /= len(self.taps)
        self.write = (self.write + n) % self.size
        self.t = int(clock[-1]) + 1
        return ((1 - self.mix) * x + self.mix * acc).astype(np.float32)


class Vocoder(Effect):
    """Channel vocoder: N band envelopes of the voice modulate the same bands of a
    synthetic carrier. The classic monotone robot — the flat pitch is most of the effect.

    16 geometric bands 150 Hz-6.5 kHz, 2nd-order Butterworth, envelope follower at 22 Hz,
    sawtooth carrier at 105 Hz. Every `sosfilt` state is carried per chunk, so the chunked
    output is bit-exact against the whole-buffer output.
    """

    def __init__(
        self,
        bands: int = 16,
        f_lo: float = 150.0,
        f_hi: float = 6500.0,
        carrier_hz: float = 105.0,
        carrier: str = "saw",
        env_hz: float = 22.0,
        noise: float = 0.25,
        sr: int = SAMPLE_RATE_HZ,
        seed: int = 1234,
    ) -> None:
        edges = np.geomspace(f_lo, f_hi, bands + 1)
        self.sos = [
            signal.butter(2, [edges[i], edges[i + 1]], btype="band", fs=sr, output="sos")
            for i in range(bands)
        ]
        self.zi_voice = [signal.sosfilt_zi(s) * 0 for s in self.sos]
        self.zi_carrier = [signal.sosfilt_zi(s) * 0 for s in self.sos]
        self.env_sos = signal.butter(2, env_hz, btype="low", fs=sr, output="sos")
        self.zi_env = [signal.sosfilt_zi(self.env_sos) * 0 for _ in self.sos]
        self.carrier_w = 2 * np.pi * carrier_hz / sr
        self.phase = 0.0
        self.kind = carrier
        self.noise = noise
        # ONE generator for the life of the stream. Re-seeding per chunk makes the
        # fricative noise identical in every chunk, which is a periodic buzz at the chunk
        # rate — 8.33 Hz at 120 ms — not the hiss it is supposed to be.
        self.rng = np.random.default_rng(seed)

    def _carrier(self, n: int) -> np.ndarray:
        phase = self.phase + self.carrier_w * np.arange(n, dtype=np.float64)
        self.phase = float((self.phase + self.carrier_w * n) % (2 * np.pi))
        ramp = phase / (2 * np.pi) % 1.0
        if self.kind == "saw":
            return (2 * ramp - 1).astype(np.float32)
        if self.kind == "pulse":
            return np.where(ramp < 0.12, 1.0, -0.15).astype(np.float32)
        return np.sign(np.sin(phase)).astype(np.float32)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if not n:
            return x
        carrier = self._carrier(n)
        # Without this noise every fricative ("s", "f", "ș") disappears and the robot
        # becomes unintelligible — the carrier has no energy where those live.
        carrier = carrier + self.noise * self.rng.standard_normal(n).astype(np.float32)
        out = np.zeros(n, np.float32)
        for k, sos in enumerate(self.sos):
            band, self.zi_voice[k] = signal.sosfilt(sos, x, zi=self.zi_voice[k])
            env, self.zi_env[k] = signal.sosfilt(self.env_sos, np.abs(band), zi=self.zi_env[k])
            excited, self.zi_carrier[k] = signal.sosfilt(sos, carrier, zi=self.zi_carrier[k])
            out += excited * env * 2.0
        return out.astype(np.float32)


class Gain(Effect):
    """Fixed makeup gain, one per preset, measured rather than guessed.

    Every one of these chains loses level — ring modulation halves it by definition, and the
    vocoder measured **14-17 dB down** on real Piper output in all three languages, which
    lands as "the robot voice is broken", not as a character. The values in
    `CHARACTER_CHAINS` restore RMS to within ~4 dB of the input while keeping the peak under
    1.0 on the measured material, so the limiter below shapes the occasional transient
    instead of working full time.

    Fixed, not an AGC: a level tracker with any smoothing constant is block-rate dependent
    unless it runs per sample, and a per-sample tracker would pump on the very material this
    is used for. A constant is chunk-size invariant for free.
    """

    def __init__(self, gain: float = 1.0) -> None:
        self.gain = gain

    def __call__(self, x: np.ndarray) -> np.ndarray:
        return (x * self.gain).astype(np.float32)


class Limiter(Effect):
    """Soft clip. Stateless, but in the chain so nothing downstream ever sees > 1.0 — the
    comb and the vocoder both add gain, and int16 wrap is a much worse sound than tanh."""

    def __init__(self, ceiling: float = 0.89) -> None:
        self.ceiling = max(ceiling, 1e-6)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        return (np.tanh(x / self.ceiling) * self.ceiling).astype(np.float32)


# --------------------------------------------------------------------------- presets

CHARACTER_CHAINS: dict[str, Callable[[], list[Effect]]] = {
    "robot_ring": lambda: [
        RingMod(50.0, 0.92), Comb(5.5, 0.55, 0.4), BitCrush(8, 2), Gain(1.3),
    ],
    "robot_dalek": lambda: [
        RingMod(30.0, 1.0), Comb(3.0, 0.7, 0.5), BitCrush(7, 3), Gain(1.3),
    ],
    "robot_vocoder": lambda: [Vocoder(16, 150.0, 6500.0, 105.0, "saw"), Gain(5.0)],
    "alien_ring": lambda: [
        RingMod(230.0, 0.45), Chorus(mix=0.55), Comb(11.0, 0.45, 0.3), Gain(2.0),
    ],
    "alien_swarm": lambda: [
        Chorus(((11.0, 0.9, 0.7), (17.0, 1.3, 1.1), (23.0, 1.7, 1.7)), mix=0.75),
        RingMod(410.0, 0.3),
        Gain(1.8),
    ],
}
"""Effect chains by name. A *factory* per name, not an instance: every stream needs its own
state, or two turns share a phase accumulator and a ring buffer and the second one starts
mid-effect. `CharacterChain` calls these once per synthesis."""


MEASURED_MS_PER_CHUNK: dict[str, float] = {
    "robot_dalek": 0.083,
    "robot_ring": 0.122,
    "alien_swarm": 0.159,
    "alien_ring": 0.341,
    "robot_vocoder": 1.009,
}
"""Median cost per 120 ms chunk, M5 Max, research/voices.md §3.1. Recorded so the test that
asserts the budget has something to regress against; the worst preset adds ~1 ms to a
57-95 ms time-to-first-audio."""


@dataclass
class CharacterChain:
    """One character effect over one stream. Build per synthesis, feed it PCM in order.

    Slices input to `MAX_BLOCK` before the effects see it, which is what lets a caller pass
    any chunk size — including the short final chunk of a sentence — without the `Chorus`
    ring buffer being wrong.
    """

    name: str

    def __post_init__(self) -> None:
        try:
            build = CHARACTER_CHAINS[self.name]
        except KeyError:
            raise ValueError(
                f"unknown character {self.name!r} (have {sorted(CHARACTER_CHAINS)})"
            ) from None
        self.effects = [*build(), Limiter()]

    def process(self, pcm: bytes) -> bytes:
        """int16 PCM in, int16 PCM out, same length. Empty in, empty out — the final
        `SynthesisChunk` carries no audio and must stay byte-empty."""
        if not pcm:
            return pcm
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        out = np.empty_like(samples)
        for start in range(0, len(samples), MAX_BLOCK):
            block = samples[start : start + MAX_BLOCK]
            for effect in self.effects:
                block = effect(block)
            out[start : start + len(block)] = block
        return (np.clip(out, -1.0, 1.0 - 1.0 / 32768.0) * 32768.0).astype("<i2").tobytes()


__all__ = [
    "CHARACTER_CHAINS",
    "MAX_BLOCK",
    "MEASURED_MS_PER_CHUNK",
    "BitCrush",
    "CharacterChain",
    "Chorus",
    "Comb",
    "Effect",
    "Gain",
    "Limiter",
    "RingMod",
    "Vocoder",
]
