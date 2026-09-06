"""Energy VAD. It must work with no model on disk, including in a room with a noise floor."""

from __future__ import annotations

from ars_protocol import SpeechBoundary
from ars_voice.audio.frames import frames_from_pcm
from ars_voice.vad.energy import EnergyVadEngine
from voice_helpers import silence, speech, stream


def run(vad: EnergyVadEngine, pcm: bytes) -> list[bool]:
    return [vad.step(frame).raw_is_speech for frame in frames_from_pcm(pcm)]


def test_speech_is_detected_and_silence_is_not():
    vad = EnergyVadEngine()
    decisions = run(vad, silence(400) + speech(600) + silence(600))
    assert not any(decisions[:15])
    assert sum(decisions[25:45]) > 15
    assert not any(decisions[-20:])


def test_adaptive_threshold_survives_a_noisy_room():
    """A fixed -42 dBFS threshold calls a loud kitchen 'speech' forever. The floating one
    tracks the floor and still finds the speech above it."""
    noisy_room = silence(1_000, seed=5)
    loud_floor = speech(12_000, dbfs=-48, seed=9)  # a television, not an utterance
    utterance = speech(800, dbfs=-24, seed=2)

    fixed = EnergyVadEngine(adaptive=False, threshold_db=-52.0)
    assert sum(run(fixed, loud_floor)) > 250, "fixed threshold: floor reads as speech"

    adaptive = EnergyVadEngine(adaptive=True, noise_margin_db=9.0)
    run(adaptive, noisy_room + loud_floor)
    assert adaptive.effective_threshold_db > -52.0, (
        "the floor never rose to meet a sustained noise source"
    )
    assert sum(run(adaptive, utterance)) > 20


def test_hangover_bridges_a_stop_consonant():
    """A 60 ms closure inside a word must not read as the end of speech."""
    vad = EnergyVadEngine(hangover_ms=200)
    smoothed = [
        vad.step(frame).is_speech
        for frame in frames_from_pcm(silence(200) + speech(400) + silence(60) + speech(400))
    ]
    middle = smoothed[len(smoothed) // 2 - 2 : len(smoothed) // 2 + 2]
    assert all(middle), "hangover did not bridge the closure"


async def test_process_emits_protocol_boundary_events():
    vad = EnergyVadEngine()
    frames = frames_from_pcm(silence(300) + speech(700) + silence(700))
    events = [event async for event in vad.process(stream(frames))]
    assert [e.boundary for e in events] == [
        SpeechBoundary.SPEECH_START,
        SpeechBoundary.SPEECH_END,
    ]
    assert events[0].energy_db is not None and events[0].energy_db > -40


def test_reset_clears_the_learned_noise_floor():
    vad = EnergyVadEngine()
    run(vad, speech(400))
    vad.reset()
    assert vad._noise_floor_db is None
    assert vad.effective_threshold_db == vad.threshold_db
