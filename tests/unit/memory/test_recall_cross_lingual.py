"""Hybrid recall must work across English and Romanian.

The hash-based mock embedding bridges languages through *shared substrings* — proper
nouns, numbers, cognates — not through learned semantics (see
`ars_memory.embeddings.hash_backend` module docstring for why). These tests are written
around that honestly: each pair of memories/queries shares an anchor token, which is
exactly the realistic case of a named place or a cognate word appearing in both an
English and a Romanian sentence. Genuine paraphrase-level cross-lingual recall (no
shared vocabulary at all) requires `SentenceTransformerEmbeddingBackend` — see
`test_sentence_transformer_backend.py`.
"""

from __future__ import annotations

from ars_protocol import (
    Language,
    MemoryKind,
    MemoryRecord,
    Provenance,
    Sensitivity,
    SourceKind,
    TrustLevel,
    now_ms,
)


def _record(text: str, language: Language, **kwargs) -> MemoryRecord:
    return MemoryRecord(
        kind=kwargs.pop("kind", MemoryKind.FACT),
        text=text,
        language=language,
        sensitivity=kwargs.pop("sensitivity", Sensitivity.PERSONAL),
        provenance=Provenance(source=SourceKind.MICROPHONE, trust=TrustLevel.USER),
        **kwargs,
    )


async def test_english_query_retrieves_romanian_memory(store):
    ro_memory = await store.remember(
        _record("Locuiesc în Cluj de trei ani.", Language.RO)
    )
    await store.remember(_record("My favorite colour is blue.", Language.EN))
    await store.remember(_record("The meeting is on Tuesday.", Language.EN))

    results = await store.recall("Where do I live, is it Cluj?", limit=3)

    assert results, "expected at least one result"
    assert results[0].id == ro_memory.id
    assert results[0].language is Language.RO


async def test_romanian_query_retrieves_english_memory(store):
    en_memory = await store.remember(
        _record("I am vegetarian and I don't eat meat.", Language.EN)
    )
    await store.remember(_record("Mașina mea este roșie.", Language.RO))
    await store.remember(_record("Am o întâlnire mâine.", Language.RO))

    results = await store.recall("Sunt vegetarian, nu mananc carne?", limit=3)

    assert results, "expected at least one result"
    assert results[0].id == en_memory.id
    assert results[0].language is Language.EN


async def test_expired_record_excluded_even_if_otherwise_best_match(store):
    live = await store.remember(_record("I live in Cluj.", Language.EN))
    expired = await store.remember(
        _record(
            "I live in Cluj temporarily this week.",
            Language.EN,
            valid_until_ms=now_ms() - 1,
        )
    )

    results = await store.recall("Cluj", limit=5)
    result_ids = {r.id for r in results}

    assert live.id in result_ids
    assert expired.id not in result_ids


async def test_language_filter_restricts_results(store):
    await store.remember(_record("Locuiesc în Cluj.", Language.RO))
    en = await store.remember(_record("I live in Cluj.", Language.EN))

    results = await store.recall("Cluj", limit=5, language=Language.EN)

    assert all(r.language is Language.EN for r in results)
    assert en.id in {r.id for r in results}
