"""Requirement 6: the `may_ask` gate is enforced at the store level, not just trusted
from the caller — a buggy caller must not be able to make A.R.S nag."""

from __future__ import annotations

import pytest
from ars_protocol import BehaviourDomain, Observation, ObservationStatus, now_ms


def _observation(**overrides) -> Observation:
    defaults = dict(
        domain=BehaviourDomain.STYLE,
        statement="Îmi răspunzi mai scurt dimineața.",
        confidence=0.8,
        evidence_turn_ids=("trn_aaaaaaaaaaaaaaaaaaaa", "trn_bbbbbbbbbbbbbbbbbbbb"),
        status=ObservationStatus.PROPOSED,
    )
    defaults.update(overrides)
    return Observation(**defaults)


async def test_pending_observations_excludes_single_evidence(store):
    weak = _observation(evidence_turn_ids=("trn_aaaaaaaaaaaaaaaaaaaa",))
    await store.observe(weak)

    pending = await store.pending_observations()

    assert weak.id not in {o.id for o in pending}


async def test_pending_observations_excludes_low_confidence(store):
    weak = _observation(confidence=0.3)
    await store.observe(weak)

    pending = await store.pending_observations()

    assert weak.id not in {o.id for o in pending}


async def test_pending_observations_includes_eligible(store):
    eligible = _observation()
    await store.observe(eligible)

    pending = await store.pending_observations()

    assert eligible.id in {o.id for o in pending}
    assert all(o.may_ask for o in pending)


async def test_store_refuses_to_mark_ineligible_observation_as_asked(store):
    weak = _observation(evidence_turn_ids=("trn_aaaaaaaaaaaaaaaaaaaa",))
    await store.observe(weak)

    asked = weak.model_copy(update={"asked_at_ms": now_ms()})
    with pytest.raises(PermissionError):
        await store.observe(asked)


async def test_store_allows_marking_eligible_observation_as_asked(store):
    eligible = _observation()
    await store.observe(eligible)

    asked = eligible.model_copy(update={"asked_at_ms": now_ms()})
    result = await store.observe(asked)

    assert result.asked_at_ms is not None


async def test_confirm_creates_preference_only_for_eligible_observation(store):
    eligible = _observation()
    await store.observe(eligible)

    preference = await store.confirm_observation(eligible.id, confirmed=True)

    assert preference is not None
    assert preference.statement == eligible.statement
    assert preference.from_observation_id == eligible.id
    active = await store.active_preferences()
    assert preference.id in {p.id for p in active}


async def test_confirm_rejects_ineligible_observation_without_creating_preference(store):
    weak = _observation(evidence_turn_ids=("trn_aaaaaaaaaaaaaaaaaaaa",))
    await store.observe(weak)

    with pytest.raises(PermissionError):
        await store.confirm_observation(weak.id, confirmed=True)

    active = await store.active_preferences()
    assert active == ()


async def test_confirm_false_rejects_without_creating_preference(store):
    eligible = _observation()
    await store.observe(eligible)

    result = await store.confirm_observation(eligible.id, confirmed=False)

    assert result is None
    active = await store.active_preferences()
    assert active == ()


async def test_confirm_unknown_observation_returns_none(store):
    result = await store.confirm_observation("obs_doesnotexist00000", confirmed=True)
    assert result is None
