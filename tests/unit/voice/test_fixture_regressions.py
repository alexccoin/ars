"""Regression lock on the endpointing tuning.

These numbers were produced by `ars-voice-endpoint-eval --sweep` on the fixture set in
data/fixtures/endpointing. They are pinned here so a future change to the VAD, the
endpointer or the defaults cannot quietly re-introduce a truncation or add 300 ms to every
turn. If one of these fails, run the sweep, look at the before/after, and decide — do not
edit the number to match.

The fixtures are synthetic (see ars_voice.fixtures): they constrain *timing* behaviour, and
say nothing about recognition accuracy.
"""

from __future__ import annotations

import pytest
from ars_voice.config import VoicePipelineConfig
from ars_voice.eval.endpointing import default_endpointer, evaluate
from ars_voice.fixtures import DEFAULT_DIR, load_specs
from ars_voice.metrics import percentile

pytestmark = pytest.mark.skipif(
    not (DEFAULT_DIR / "endpointing" / "manifest.json").is_file(),
    reason="fixtures not generated — run `ars-voice-fixtures all`",
)


def test_the_fixture_set_covers_both_languages_and_both_failure_modes():
    specs = [spec for spec, _ in load_specs("endpointing")]
    languages = {spec.language.value for spec in specs}
    assert languages == {"en", "ro"}, "a set that is only English lets RO regressions through"
    # Half the set punishes a short window (internal pauses), half punishes a long one.
    with_pauses = [s for s in specs if sum(1 for seg in s.segments if seg.kind == "speech") > 1]
    assert len(with_pauses) >= 4
    assert len(specs) - len(with_pauses) >= 4


def test_shipped_defaults_never_cut_the_user_off():
    result = evaluate("shipped default", endpointer_factory=default_endpointer)
    assert result.truncations == 0, [r.name for r in result.results if r.truncated]
    assert result.missed == 0


def test_endpoint_latency_is_the_configured_window_plus_a_frame():
    config = VoicePipelineConfig()
    result = evaluate("shipped default", endpointer_factory=default_endpointer)
    p95 = percentile(result.latencies, 95)
    assert p95 == pytest.approx(config.core.endpoint_silence_ms, abs=40), (
        f"p95 endpoint latency {p95:.0f} ms drifted from the {config.core.endpoint_silence_ms} ms "
        "silence window — something is adding a wait that is not the configured one"
    )


@pytest.mark.parametrize("silence_ms", [300, 400])
def test_a_shorter_window_is_measurably_worse(silence_ms: int):
    """The evidence for keeping 700 ms. These configurations are faster and they cut the
    user off; the fixture set is what makes that a fact rather than an opinion."""
    result = evaluate(
        f"{silence_ms} ms",
        endpointer_factory=lambda: default_endpointer(silence_ms=float(silence_ms), adaptive=False),
    )
    assert result.truncations > 0


def test_adaptive_endpointing_stays_off_because_it_truncates_long_thoughts():
    """Adaptive saves ~280 ms at p50 and truncates two fixtures — both of them a user
    pausing between two clauses. That trade is the wrong way round, so it ships off."""
    adaptive = evaluate("adaptive", endpointer_factory=lambda: default_endpointer(adaptive=True))
    truncated = sorted(r.name for r in adaptive.results if r.truncated)
    assert truncated == ["en_long_thought", "ro_long_thought"]
    assert VoicePipelineConfig().endpointing.adaptive is False


def test_hesitation_handling_is_what_makes_adaptive_survivable_at_all():
    """Without the transcript, adaptive cannot tell a finished sentence from a trailing
    'and', so it falls back to the safe window everywhere and buys nothing."""
    blind = evaluate(
        "adaptive, no partials",
        endpointer_factory=lambda: default_endpointer(adaptive=True),
        use_partials=False,
    )
    informed = evaluate(
        "adaptive, partials",
        endpointer_factory=lambda: default_endpointer(adaptive=True),
    )
    assert percentile(informed.latencies, 50) < percentile(blind.latencies, 50)
