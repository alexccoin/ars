"""Streaming character-voice DSP over 16 kHz mono int16 PCM, 120 ms chunks.

RESEARCH PROTOTYPE. Not wired into services/voice. It exists so the numbers in
research/voices.md are reproducible, and so that a future `ars_voice.tts.character`
has a reference implementation that is already known to stream correctly.

Every effect here is causal, carries its own state across chunks and uses no lookahead,
which is what makes it usable between PiperTtsEngine and the SynthesisChunk it yields.
Verified on 2026-09-06:

* chunk-size invariance: identical output at 320 / 640 / 1920 sample chunks to 2e-6
  relative RMS, i.e. the state really is carried and the effect does not depend on the
  chunk grid;
* no boundary clicks: max |x[n]-x[n-1]| at a chunk boundary is never larger than the
  99.99th percentile of interior sample deltas;
* cost per 120 ms chunk on an M5 Max: 0.08 ms (dalek) to 1.01 ms (vocoder), i.e. at
  worst 0.84% of realtime.

Two bugs were found by those checks and are fixed below. Both are easy to reintroduce:

1. `Chorus`'s ring buffer must be at least chunk + longest delay. Sized to the delay
   alone, the write of chunk k overwrites samples the taps of chunk k still have to read.
2. `Vocoder`'s fricative noise needs ONE generator for the life of the stream. Re-seeding
   per chunk makes the noise repeat every 120 ms, which is audible as a buzz.

Writes WAV files to disk only. No audio device is ever opened by this module.
"""
from __future__ import annotations
import time, pathlib, wave
import numpy as np
from scipy import signal

SR = 16000
CHUNK = 1920            # 120 ms @ 16 kHz, == bytes_for_ms(120)//2

# ---------------------------------------------------------------- primitives

class RingMod:
    """x * (1-mix + mix*cos(wt)). Phase carried across chunks or you get a click
    at every chunk boundary."""
    def __init__(self, freq=50.0, mix=1.0, sr=SR):
        self.w = 2*np.pi*freq/sr; self.mix = mix; self.phase = 0.0
    def __call__(self, x):
        n = len(x)
        ph = self.phase + self.w*np.arange(n, dtype=np.float32)
        self.phase = float((self.phase + self.w*n) % (2*np.pi))
        return x*((1.0-self.mix) + self.mix*np.cos(ph, dtype=np.float32))

class BitCrush:
    """Quantise to `bits` and sample-and-hold every `hold` samples.
    `hold` needs a cross-chunk offset or the held grid jitters per chunk."""
    def __init__(self, bits=8, hold=2):
        self.q = 2.0**(bits-1); self.hold = hold; self.off = 0; self.last = 0.0
    def __call__(self, x):
        y = np.round(x*self.q)/self.q
        if self.hold > 1:
            n = len(y); idx = (np.arange(n)+self.off)//self.hold*self.hold - self.off
            first = idx < 0
            out = np.empty_like(y)
            out[~first] = y[idx[~first]]
            out[first] = self.last
            self.off = (self.off+n) % self.hold
            self.last = float(y[idx[-1]]) if idx[-1] >= 0 else self.last
            y = out
        return y

class Comb:
    """Feedback comb = IIR y[n] = x[n] + fb*y[n-d]. Expressed as an lfilter so it is
    vectorised; `zi` carries the tail across chunks, which is what makes it streamable."""
    def __init__(self, delay_ms=6.0, fb=0.6, mix=0.5, sr=SR):
        self.d = max(1, int(delay_ms*sr/1000))
        self.a = np.zeros(self.d+1); self.a[0] = 1.0; self.a[self.d] = -fb
        self.b = np.array([1.0])
        self.zi = np.zeros(self.d)
        self.mix = mix
    def __call__(self, x):
        out, self.zi = signal.lfilter(self.b, self.a, x, zi=self.zi)
        return ((1-self.mix)*x + self.mix*out).astype(np.float32)

class Chorus:
    """N LFO-modulated delay taps -> detuned 'many voices at once'."""
    def __init__(self, taps=((14.0,0.31,2.6),(19.0,0.19,3.4),(25.0,0.13,4.1)), mix=0.6, sr=SR,
                 chunk=CHUNK):
        # The ring buffer must hold one whole chunk PLUS the longest delay, otherwise the
        # write of chunk k overwrites samples that the taps of chunk k still need to read.
        longest = max(b+d for b,d,_ in taps)
        self.max = int(chunk + longest*sr/1000) + 4
        self.buf = np.zeros(self.max, np.float32)
        self.w = 0; self.taps = taps; self.mix = mix; self.sr = sr; self.t = 0
    def __call__(self, x):
        n = len(x); sr = self.sr
        # write into ring buffer
        idx = (self.w + np.arange(n)) % self.max
        self.buf[idx] = x
        tn = self.t + np.arange(n)
        acc = np.zeros(n, np.float32)
        for base_ms, depth_ms, rate in self.taps:
            d = (base_ms + depth_ms*np.sin(2*np.pi*rate*tn/sr))*sr/1000.0
            r = (self.w + np.arange(n) - d) % self.max
            i0 = r.astype(np.int32); frac = (r-i0).astype(np.float32)
            i1 = (i0+1) % self.max
            acc += (1-frac)*self.buf[i0] + frac*self.buf[i1]
        acc /= len(self.taps)
        self.w = (self.w+n) % self.max; self.t = tn[-1]+1
        return (1-self.mix)*x + self.mix*acc

class Vocoder:
    """Channel vocoder: N bandpass envelope followers modulate the same bands of a
    synthetic carrier. sosfilt zi state is carried per chunk, so it streams exactly."""
    def __init__(self, bands=16, f_lo=150., f_hi=6500., carrier_hz=110.0,
                 carrier="saw", env_hz=22.0, sr=SR):
        edges = np.geomspace(f_lo, f_hi, bands+1)
        self.sos = [signal.butter(2, [edges[i], edges[i+1]], btype="band", fs=sr, output="sos")
                    for i in range(bands)]
        self.zi_m = [signal.sosfilt_zi(s)*0 for s in self.sos]
        self.zi_c = [signal.sosfilt_zi(s)*0 for s in self.sos]
        self.env_sos = signal.butter(2, env_hz, btype="low", fs=sr, output="sos")
        self.zi_e = [signal.sosfilt_zi(self.env_sos)*0 for _ in self.sos]
        self.carrier_w = 2*np.pi*carrier_hz/sr; self.phase = 0.0
        self.kind = carrier; self.sr = sr
        # ONE generator for the life of the stream. Re-seeding per chunk makes the
        # fricative noise repeat every 120 ms, which is audible as a buzz.
        self.rng = np.random.default_rng(1234)
    def _carrier(self, n):
        ph = self.phase + self.carrier_w*np.arange(n)
        self.phase = float((self.phase + self.carrier_w*n) % (2*np.pi))
        t = ph/(2*np.pi) % 1.0
        if self.kind == "saw":   return (2*t-1).astype(np.float32)
        if self.kind == "pulse": return np.where(t < 0.12, 1.0, -0.15).astype(np.float32)
        return np.sign(np.sin(ph)).astype(np.float32)
    def __call__(self, x):
        c = self._carrier(len(x))
        # a little noise keeps fricatives ("s", "f") intelligible
        c = c + 0.25*self.rng.standard_normal(len(x)).astype(np.float32)
        out = np.zeros(len(x), np.float32)
        for k, sos in enumerate(self.sos):
            m, self.zi_m[k] = signal.sosfilt(sos, x, zi=self.zi_m[k])
            e, self.zi_e[k] = signal.sosfilt(self.env_sos, np.abs(m), zi=self.zi_e[k])
            b, self.zi_c[k] = signal.sosfilt(sos, c, zi=self.zi_c[k])
            out += b*e*2.0
        return out

def limiter(x, ceiling=0.89):
    return np.tanh(x/max(ceiling, 1e-6)).astype(np.float32)*ceiling

# ---------------------------------------------------------------- presets

def chain(name):
    if name == "robot_ring":
        fx = [RingMod(50.0, 0.92), Comb(5.5, 0.55, 0.4), BitCrush(8, 2)]
    elif name == "robot_vocoder":
        fx = [Vocoder(16, 150, 6500, 105.0, "saw")]
    elif name == "robot_dalek":
        fx = [RingMod(30.0, 1.0), Comb(3.0, 0.7, 0.5), BitCrush(7, 3)]
    elif name == "alien_ring":
        fx = [RingMod(230.0, 0.45), Chorus(mix=0.55), Comb(11.0, 0.45, 0.3)]
    elif name == "alien_swarm":
        fx = [Chorus(((11.0,0.9,0.7),(17.0,1.3,1.1),(23.0,1.7,1.7)), mix=0.75),
              RingMod(410.0, 0.3)]
    elif name == "none":
        fx = []
    else:
        raise ValueError(name)
    def run(x):
        for f in fx: x = f(x)
        return limiter(x) if fx else x
    return run, fx

# ---------------------------------------------------------------- harness

def pcm_to_f32(b): return np.frombuffer(b, "<i2").astype(np.float32)/32768.0
def f32_to_pcm(x): return (np.clip(x, -1, 1)*32767).astype("<i2").tobytes()

def process_stream(pcm_bytes, preset):
    run, _ = chain(preset)
    out = bytearray(); costs = []
    for off in range(0, len(pcm_bytes), CHUNK*2):
        c = pcm_bytes[off:off+CHUNK*2]
        t = time.perf_counter()
        y = run(pcm_to_f32(c))
        costs.append((time.perf_counter()-t)*1000)
        out += f32_to_pcm(y)
    return bytes(out), costs

def write_wav(path, pcm, sr=SR):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm)
