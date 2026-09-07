"""Robot and alien voices: the DSP, and the engine wrapper that streams it.

These are effects on a live audio stream, so the interesting failures are not "does it
sound like a robot" — they are the ones that make it sound like a *broken* robot: state
that does not carry across chunk boundaries, a ring buffer that eats its own tail, noise
that repeats at the chunk rate. Every one of those is silent in a spectrogram and obvious
in a room, so each has a test.

Nothing here needs model weights except the two marked otherwise: the whole point of the
wrapper design is that the character is testable over `MockTtsEngine`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import statistics
import time
from pathlib import Path

import numpy as np
import pytest
from ars_protocol import SAMPLE_RATE_HZ, SUPPORTED_LANGUAGES, Language, SynthesisRequest
from ars_voice.audio.synth import formants_for, speech_like_pcm
from ars_voice.metrics import BUDGET_P95_MS, Stage
from ars_voice.tts.catalogue import CHARACTERS
from ars_voice.tts.character import CharacterTtsEngine
from ars_voice.tts.dsp import CHARACTER_CHAINS, MAX_BLOCK, CharacterChain, Chorus
from ars_voice.tts.mock import MockTtsEngine

ROOT = Path(__file__).resolve().parents[3]
MODELS = ROOT / "models" / "tts"

needs_piper = pytest.mark.skipif(
    importlib.util.find_spec("piper") is None or not (MODELS / "en_US-amy-medium.onnx").is_file(),
    reason="piper package or voices missing",
)

CHUNK_SAMPLES = int(SAMPLE_RATE_HZ * 0.12)


def voiced_pcm(seconds: float = 1.5, language: Language = Language.EN) -> bytes:
    """The suite's own speech-shaped generator: harmonics, formants, a syllable envelope and
    breath noise.

    A bare harmonic stack is the wrong fixture here and gave the wrong answer once already —
    with all its energy at one fundamental it made the vocoder look 14 dB quiet, because
    real speech has broadband energy the vocoder's 150 Hz-6.5 kHz bands can actually see.
    """
    return speech_like_pcm(
        seconds * 1000.0, formants=formants_for(language.value), seed=7, amplitude_dbfs=-22.0
    )


def through(preset: str, pcm: bytes, samples_per_call: int) -> np.ndarray:
    chain = CharacterChain(preset)
    step = samples_per_call * 2
    out = b"".join(chain.process(pcm[o : o + step]) for o in range(0, len(pcm), step))
    return np.frombuffer(out, "<i2").astype(np.float64)


# --------------------------------------------------------------------------- the DSP

@pytest.mark.parametrize("preset", sorted(CHARACTER_CHAINS))
def test_the_same_audio_comes_out_at_every_chunk_size(preset: str):
    """`TtsConfig.chunk_ms` is a latency knob owned by the pipeline, and the final chunk of
    a sentence is short whatever it is set to. If the effect's output depends on the chunk
    grid then every one of those is a different voice, and retuning latency silently retunes
    the character. 997 is in the list precisely because nothing about it is round."""
    pcm = voiced_pcm()
    reference = through(preset, pcm, CHUNK_SAMPLES)
    for samples in (320, 640, 997, MAX_BLOCK):
        got = through(preset, pcm, samples)
        error = np.sqrt(np.mean((got - reference) ** 2)) / max(np.sqrt(np.mean(reference**2)), 1e-9)
        assert error < 1e-5, f"{preset} differs at {samples} samples/chunk (rel RMS {error:.2e})"


@pytest.mark.parametrize("preset", sorted(CHARACTER_CHAINS))
def test_chunk_boundaries_do_not_click(preset: str):
    """A phase accumulator or filter state reset per chunk produces a discontinuity at every
    boundary — at 120 ms chunks that is a 8.33 Hz buzz laid over the voice, which reads as
    'the robot effect is broken' rather than as an effect. The test: the biggest sample-to-
    sample jump across a boundary is no larger than the jumps the effect makes anyway."""
    y = through(preset, voiced_pcm(), CHUNK_SAMPLES)
    deltas = np.abs(np.diff(y))
    boundaries = deltas[np.arange(CHUNK_SAMPLES - 1, len(y) - 1, CHUNK_SAMPLES)]
    assert boundaries.max() <= np.percentile(deltas, 99.99)


def test_the_chorus_ring_buffer_holds_a_whole_block_plus_the_longest_delay():
    """Regression, and the bug is easy to reintroduce because the wrong version still sounds
    plausible: sized to the delay alone, the write of block k overwrites samples the taps of
    block k have not read yet. Here an undersized chorus is built deliberately, and must
    disagree with the correct one — which is the proof the sizing is load-bearing."""
    pcm = voiced_pcm(0.5)
    samples = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768.0

    correct = Chorus()
    undersized = Chorus(max_block=64)
    assert correct.size >= MAX_BLOCK + int(25.0 * SAMPLE_RATE_HZ / 1000)

    block = samples[:MAX_BLOCK]
    assert not np.allclose(correct(block.copy()), undersized(block.copy()))

    piecewise = Chorus()
    small = np.concatenate([piecewise(samples[o : o + 240]) for o in range(0, MAX_BLOCK, 240)])
    assert np.allclose(Chorus()(samples[:MAX_BLOCK]), small, atol=1e-6)


def test_the_vocoder_noise_does_not_repeat_at_the_chunk_rate():
    """Regression. The vocoder adds white noise to its carrier so fricatives survive; with
    one RNG per chunk that 'noise' is the same 120 ms of noise over and over, which is a
    periodic tone at the chunk rate rather than hiss. One generator for the life of the
    stream is the fix, and the observable consequence is that feeding identical audio twice
    does not produce identical output."""
    block = np.frombuffer(voiced_pcm(0.12), "<i2").tobytes()
    streaming = CharacterChain("robot_vocoder")
    first = np.frombuffer(streaming.process(block), "<i2").astype(float)
    second = np.frombuffer(streaming.process(block), "<i2").astype(float)
    assert not np.allclose(first, second)

    reseeded = np.frombuffer(CharacterChain("robot_vocoder").process(block), "<i2").astype(float)
    assert np.allclose(first, reseeded), "a fresh chain must be deterministic, for renders"


@pytest.mark.parametrize("preset", sorted(CHARACTER_CHAINS))
def test_a_character_is_not_dramatically_quieter_than_the_voice_it_replaces(preset: str):
    """Measured before the makeup gains existed: the vocoder came out 14-17 dB below the
    voice it was processing. A character the user has to reach for the volume knob for is a
    bug report about volume, not a character."""
    pcm = voiced_pcm()
    source = np.frombuffer(pcm, "<i2").astype(np.float64)
    out = through(preset, pcm, CHUNK_SAMPLES)
    delta_db = 20 * np.log10(
        np.sqrt(np.mean(out**2)) / max(np.sqrt(np.mean(source**2)), 1e-9)
    )
    assert -7.0 < delta_db < 3.0, f"{preset} is {delta_db:+.1f} dB against the plain voice"
    assert np.abs(out).max() < 32768, "clipped"


@pytest.mark.parametrize("preset", sorted(CHARACTER_CHAINS))
def test_the_whole_sci_fi_register_costs_about_a_millisecond_a_chunk(preset: str):
    """The argument for doing characters as DSP instead of as more models rests on this
    number. Measured 0.08-1.01 ms per 120 ms chunk; the assertion is deliberately loose (a
    tenth of the chunk) so it fails on an algorithmic mistake — a per-sample Python loop, a
    filter rebuilt per call — and not on a busy CI machine."""
    pcm = voiced_pcm(2.0)
    chain = CharacterChain(preset)
    step = CHUNK_SAMPLES * 2
    costs = []
    for offset in range(0, len(pcm), step):
        started = time.perf_counter()
        chain.process(pcm[offset : offset + step])
        costs.append((time.perf_counter() - started) * 1000)
    assert statistics.median(costs) < 12.0, f"{preset} median {statistics.median(costs):.2f} ms"


# --------------------------------------------------------------------------- the wrapper

async def test_a_plain_voice_passes_through_the_wrapper_untouched():
    """The wrapper is on the path whether or not a character is in use, so 'no character'
    has to be bit-identical to not having it there at all."""
    inner = MockTtsEngine(chunk_ms=120, realtime_factor=200.0)
    plain = [
        c.pcm
        async for c in inner.synthesize(
            SynthesisRequest(text="Turn the lights off.", language=Language.EN)
        )
    ]
    wrapped = [
        c.pcm
        async for c in CharacterTtsEngine(inner).synthesize(
            SynthesisRequest(text="Turn the lights off.", language=Language.EN)
        )
    ]
    assert plain == wrapped


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
async def test_every_character_works_in_every_language(language: Language):
    """The decisive property, and the reason this is DSP and not a model: Romanian has one
    voice and will never have a robot model trained for it. An effect applies to whatever is
    speaking, so `robot_dalek` exists in Romanian the day it exists in English."""
    engine = CharacterTtsEngine(MockTtsEngine(chunk_ms=120, realtime_factor=200.0))
    for character in CHARACTERS:
        request = SynthesisRequest(
            text="Turn the lights off.", language=language, voice=f"{character}/whatever"
        )
        chunks = [c async for c in engine.synthesize(request)]
        assert sum(len(c.pcm) for c in chunks) > 0
        assert engine.last_character == character


async def test_the_character_changes_the_audio_but_not_the_chunk_contract():
    """Sequence numbers and the empty final chunk are the sink's contract: a non-empty final
    is counted as audio, and a gap in `seq` is counted as a dropped chunk. An effect that
    rewrote either would corrupt playback in a way that has nothing to do with how it
    sounds."""
    inner = MockTtsEngine(chunk_ms=120, realtime_factor=200.0)
    engine = CharacterTtsEngine(inner, character="robot_dalek")
    request = SynthesisRequest(text="Turn the lights off. Done.", language=Language.EN)
    chunks = [c async for c in engine.synthesize(request)]

    assert [c.seq for c in chunks] == list(range(len(chunks)))
    assert chunks[-1].is_final and chunks[-1].pcm == b""
    assert all(len(c.pcm) for c in chunks[:-1])
    assert engine.characters_applied == 1
    plain = [c.pcm async for c in inner.synthesize(request)]
    assert [c.pcm for c in chunks] != plain


async def test_the_configured_character_survives_a_round_trip_through_voice_for():
    """`StreamingSynthesizer` fills an empty request with `engine.voice_for(language)`, so a
    persona configured on the engine has to come back out of that call or it is dropped on
    every turn the caller did not name a voice explicitly."""
    engine = CharacterTtsEngine(MockTtsEngine(), character="alien_swarm")
    for language in SUPPORTED_LANGUAGES:
        spec = engine.voice_for(language)
        assert spec.startswith("alien_swarm/")
        request = SynthesisRequest(text="Hello there.", language=language, voice=spec)
        chunks = [c async for c in engine.synthesize(request)]
        assert engine.last_character == "alien_swarm"
        assert sum(len(c.pcm) for c in chunks) > 0


async def test_cancel_reaches_the_engine_underneath():
    """Barge-in has to stop *generation*. A wrapper that swallowed cancel would leave the
    real synthesiser running and merely stop transforming its output, which is the polite
    version of not cancelling at all."""
    inner = MockTtsEngine(chunk_ms=120, realtime_factor=4.0)
    engine = CharacterTtsEngine(inner, character="robot_ring")
    long_reply = " ".join(["This is a fairly long sentence that takes a while to speak."] * 6)
    produced = 0

    async def consume():
        nonlocal produced
        async for _ in engine.synthesize(
            SynthesisRequest(text=long_reply, language=Language.EN)
        ):
            produced += 1

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.08)
    at_cancel = produced
    await engine.cancel()
    await task

    assert inner.cancellations == 1
    assert at_cancel > 0, "test never reached the speaking phase"
    assert produced - at_cancel <= 1, "generation continued after cancel"


def test_the_catalogue_and_the_dsp_agree_on_which_characters_exist():
    """`CHARACTERS` carries the names, kinds and trilingual descriptions; `CHARACTER_CHAINS`
    carries the signal processing. They are separate files so the DSP stays free of product
    copy, which means they can drift - a character with a description and no chain resolves
    to nothing, and one with a chain and no description is unpickable and untranslated."""
    assert set(CHARACTERS) == set(CHARACTER_CHAINS)
    assert {c.kind for c in CHARACTERS.values()} == {"robot", "alien"}


def test_an_unknown_character_is_refused_at_construction():
    with pytest.raises(ValueError, match="unknown character"):
        CharacterTtsEngine(MockTtsEngine(), character="wizard")
    with pytest.raises(ValueError, match="unknown character"):
        CharacterChain("wizard")


# --------------------------------------------------------------------------- real weights

@needs_piper
@pytest.mark.parametrize(
    ("language", "text"),
    [
        (Language.EN, "Good morning, I found three new messages from the bank."),
        (Language.RO, "Bună dimineața, am găsit trei mesaje noi de la bancă."),
        (Language.DE, "Guten Morgen, ich habe drei neue Nachrichten von der Bank."),
    ],
)
async def test_a_character_voice_still_meets_the_first_audio_budget(
    language: Language, text: str
):
    """The expensive preset over a real voice, in every language. The DSP adds ~1 ms to a
    57-95 ms synthesis; if this ever fails it is because something acquired a lookahead or
    started buffering, not because the arithmetic got slower."""
    from ars_voice.tts.piper import PiperTtsEngine

    engine = CharacterTtsEngine(
        PiperTtsEngine(model_dir=MODELS, first_sentence_max_chars=90),
        character="robot_vocoder",
    )
    request = SynthesisRequest(text=text, language=language)
    async for chunk in engine.synthesize(request):  # warm the voice
        if chunk.pcm:
            break
    samples = []
    for _ in range(3):
        started = time.perf_counter()
        async for chunk in engine.synthesize(request):
            if chunk.pcm:
                samples.append((time.perf_counter() - started) * 1000)
                break
    median = statistics.median(samples)
    assert median < BUDGET_P95_MS[Stage.TTS_FIRST_AUDIO], f"{language.value} {median:.0f} ms"


@needs_piper
async def test_the_character_of_a_real_voice_is_click_free_at_the_chunk_boundaries():
    """Chunk-size invariance is checked on synthetic audio because it must hold exactly;
    this checks the same property on real speech, where the boundaries land in the middle of
    vowels and fricatives rather than on a periodic test tone."""
    from ars_voice.tts.piper import PiperTtsEngine

    inner = PiperTtsEngine(model_dir=MODELS, chunk_ms=120)
    engine = CharacterTtsEngine(inner, character="alien_swarm")
    pcm = bytearray()
    boundaries: list[int] = []
    async for chunk in engine.synthesize(
        SynthesisRequest(text="Bună dimineața, am găsit trei mesaje noi.", language=Language.RO)
    ):
        if chunk.pcm:
            pcm += chunk.pcm
            boundaries.append(len(pcm) // 2)
    y = np.frombuffer(bytes(pcm), "<i2").astype(np.float64)
    deltas = np.abs(np.diff(y))
    at_boundary = [deltas[b - 1] for b in boundaries[:-1] if 0 < b - 1 < len(deltas)]
    assert at_boundary
    assert max(at_boundary) <= np.percentile(deltas, 99.99)
