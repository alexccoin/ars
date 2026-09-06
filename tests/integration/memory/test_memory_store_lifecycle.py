"""Integration test: `services/memory` as the `ars_core.interfaces.MemoryStore` seam
that `services/compute` plugs into. This crosses the service boundary at the interface
level (compute never imports `ars_memory` directly, only `ars_core.interfaces.
MemoryStore` — see CLAUDE.md rule 2) and at the persistence level: the store is closed
and reopened against the same on-disk file, the way a real process restart would, to
prove migrations are idempotent and nothing lives only in memory.
"""

from __future__ import annotations

from pathlib import Path

from ars_core.interfaces import MemoryStore
from ars_memory import MemoryConfig, SqliteMemoryStore
from ars_memory.retention import RetentionSweeper
from ars_protocol import (
    BehaviourDomain,
    Language,
    MemoryKind,
    MemoryRecord,
    Observation,
    ObservationStatus,
    Provenance,
    Sensitivity,
    SourceKind,
    TrustLevel,
)


def _fact(text: str, language: Language = Language.EN) -> MemoryRecord:
    return MemoryRecord(
        kind=MemoryKind.FACT,
        text=text,
        language=language,
        sensitivity=Sensitivity.PERSONAL,
        provenance=Provenance(source=SourceKind.MICROPHONE, trust=TrustLevel.USER),
    )


async def test_sqlite_memory_store_satisfies_the_memory_store_interface(tmp_path: Path):
    config = MemoryConfig(data_dir=tmp_path, embedding_backend="hash")
    store = await SqliteMemoryStore.open(config)
    try:
        assert isinstance(store, MemoryStore)
    finally:
        await store.close()


async def test_full_lifecycle_survives_a_process_restart(tmp_path: Path):
    config = MemoryConfig(data_dir=tmp_path, embedding_backend="hash")

    # --- "process 1": remember, correct, learn a behaviour ---
    store = await SqliteMemoryStore.open(config)
    old = await store.remember(_fact("I live in Cluj"))
    new = await store.remember(_fact("I moved to Bucharest"))
    await store.remember(old.model_copy(update={"superseded_by": new.id}))

    ro_fact = await store.remember(_fact("Sunt vegetarian.", Language.RO))

    observation = Observation(
        domain=BehaviourDomain.STYLE,
        statement="Îmi răspunzi mai scurt dimineața.",
        confidence=0.9,
        evidence_turn_ids=("t1", "t2", "t3"),
        status=ObservationStatus.PROPOSED,
    )
    await store.observe(observation)
    preference = await store.confirm_observation(observation.id, confirmed=True)
    assert preference is not None

    await store.close()

    # --- "process 2": reopen the same file, expect everything intact ---
    store2 = await SqliteMemoryStore.open(config)
    try:
        current = await store2.recall("Where do I live?", limit=5)
        current_ids = {r.id for r in current}
        assert new.id in current_ids
        assert old.id not in current_ids

        history = await store2.history(new.id)
        assert {r.id for r in history} == {old.id, new.id}

        cross_lingual = await store2.recall("Am I vegetarian?", limit=5)
        assert any(r.id == ro_fact.id for r in cross_lingual) or any(
            "vegetarian" in r.text.lower() for r in cross_lingual
        )

        preferences = await store2.active_preferences()
        assert preference.id in {p.id for p in preferences}

        pending = await store2.pending_observations()
        assert observation.id not in {o.id for o in pending}  # already confirmed
    finally:
        await store2.close()


async def test_forget_and_retention_survive_a_restart(tmp_path: Path):
    config = MemoryConfig(data_dir=tmp_path, embedding_backend="hash")
    now = 10_000_000_000

    store = await SqliteMemoryStore.open(config)
    keep = await store.remember(_fact("I live in Cluj"))
    gone = MemoryRecord(
        kind=MemoryKind.FACT,
        text="temporary note",
        language=Language.EN,
        sensitivity=Sensitivity.PUBLIC,
        provenance=Provenance(source=SourceKind.MICROPHONE, trust=TrustLevel.USER),
        created_at_ms=now - 1000,
        valid_until_ms=now - 1,
    )
    await store.remember(gone)
    await store.close()

    store2 = await SqliteMemoryStore.open(config)
    try:
        report = await RetentionSweeper(store2, config).sweep(at_ms=now)
        assert report.expired_valid_until == 1
        assert await store2.debug_rowid_for(gone.id) is None
        assert await store2.debug_rowid_for(keep.id) is not None
    finally:
        await store2.close()
