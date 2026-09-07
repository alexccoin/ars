"""A.R.S must not answer itself.

Run on a laptop with its own speakers, it did. It spoke, its own output crossed the
barge-in threshold, it cancelled its turn as though interrupted, resumed listening with
the echo as pre-roll, transcribed itself — "It's...", "Please.", "Search." — and answered.
Five times, escalating, until the microphone was closed from outside.

The mitigations that were supposed to prevent it are in `BargeInConfig` and both were
already there: sustained-speech duration and a raised energy threshold while the speaker
is active. Neither survives a speaker 30 cm from a microphone. It is not a tuning problem
and these tests do not treat it as one.
"""

from __future__ import annotations

import pytest
from ars_voice.config import BargeInConfig, VoicePipelineConfig


def test_barge_in_is_off_unless_someone_turns_it_on() -> None:
    """A laptop is half-duplex whether we admit it or not. Listening while speaking, into
    an open microphone, with no echo cancellation, is a feedback loop with extra steps."""
    assert BargeInConfig().enabled is False
    assert VoicePipelineConfig().barge_in.enabled is False


def test_the_thresholds_still_exist_for_when_it_is_on() -> None:
    """Turning it back on — with a headset, or once AEC exists — must not require
    rediscovering the tuning. The numbers stay; only the default changed."""
    cfg = BargeInConfig(enabled=True)

    assert cfg.min_speech_ms >= 20
    assert cfg.energy_margin_db > 0
    assert cfg.resume_listening is True


def test_interrupting_never_needed_the_microphone() -> None:
    """The reason turning barge-in off is acceptable: a turn can still be cut short.

    The stop button, Escape and a `{"type": "interrupt"}` on the socket all reach
    `VoicePipeline.interrupt`, which takes no audio and consults no threshold. If that
    ever stopped being true, disabling barge-in would leave the user with no way to stop
    A.R.S talking — which is worse than the echo problem it was disabled for.

    Asserted from the signature rather than the source text: an earlier version of this
    test grepped the method body and broke the moment someone reworded a docstring, which
    is a test failing for a reason that has nothing to do with the behaviour it guards.
    """
    import inspect

    from ars_voice.pipeline import VoicePipeline

    signature = inspect.signature(VoicePipeline.interrupt)
    assert set(signature.parameters) <= {"self", "reason"}, (
        "interrupt must need nothing but a reason — no audio, no threshold, no config"
    )


# ------------------------------------------------------------------ push to talk

def test_a_press_survives_the_stream_starting() -> None:
    """`BufferedWakewordEngine.detect` resets at the start of every stream — correct for
    pre-roll, refractory and window state, all of which belong to one stream. An arm does
    not: it is a person pressing a button, and the gateway presses as soon as it has asked
    the loop to start, which is before the stream exists.

    Clearing it there discarded every press microseconds after it was made. The microphone
    opened, delivered audio perfectly, and waited for a keyword that could never arrive —
    which from outside is indistinguishable from a broken microphone."""
    from ars_voice.wakeword.manual import ManualWakewordEngine

    engine = ManualWakewordEngine()
    engine.trigger()
    engine.reset()

    assert engine._armed, "the press was thrown away by the stream starting"


def test_an_unpressed_engine_stays_silent_across_a_reset() -> None:
    """The fix must not arm anything by itself. A wakeword that fires without being asked
    starts a turn nobody wanted, with the microphone already open."""
    from ars_voice.wakeword.manual import ManualWakewordEngine

    engine = ManualWakewordEngine()
    engine.reset()

    assert not engine._armed


@pytest.mark.asyncio
async def test_a_press_fires_exactly_once() -> None:
    """One press, one turn. If the arm survived firing, releasing the button would leave
    A.R.S starting turns forever."""
    from ars_voice.wakeword.manual import ManualWakewordEngine

    engine = ManualWakewordEngine()
    engine.trigger()

    assert await engine._score_window(b"\x00\x00" * 640) == 1.0
    assert await engine._score_window(b"\x00\x00" * 640) == 0.0
