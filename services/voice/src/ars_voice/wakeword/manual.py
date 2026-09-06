"""Push-to-talk: the turn starts because the user asked for it, not because a model heard
its name.

A wakeword engine is the right shape for this even though no keyword is involved. The
pipeline begins a turn on a `WakeEvent` and nothing else, and that event carries the
pre-roll — the half second of audio recorded *before* the trigger. That matters more here
than for a spoken keyword, not less: people start talking as they press, so without the
pre-roll the first syllable is already gone by the time the stream is armed.

Implementing this anywhere but here would mean a second way to start a turn, and two ways
to start a turn is how one of them ends up not cancelling properly.
"""

from __future__ import annotations

from .base import BufferedWakewordEngine


class ManualWakewordEngine(BufferedWakewordEngine):
    """Fires once per `trigger()`, on the next inference window.

    False accepts are not a meaningful measurement for this engine — it fires only when a
    human presses a button, so its FA/hour is zero by construction rather than by
    evaluation. That is why it does not carry a benchmark record and why nothing here
    warns about a missing one.
    """

    name = "manual"

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("keyword", "push_to_talk")
        # A press is unambiguous, so a press must never be swallowed by the gap that
        # exists to stop one spoken keyword firing three times.
        kwargs.setdefault("refractory_ms", 0)
        super().__init__(**kwargs)
        self._armed = False

    def trigger(self) -> None:
        """Arm one detection. Safe to call from any thread — it sets a flag."""
        self._armed = True

    async def _score_window(self, pcm: bytes) -> float:
        if not self._armed:
            return 0.0
        self._armed = False
        return 1.0

    def reset(self) -> None:
        super().reset()
        self._armed = False
