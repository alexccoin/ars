"""Documents are indexed in every language A.R.S speaks, not only their own.

This is the fix for the document tier's cross-language hole. The tier matches a question
to a passage with an embedding model that cannot bridge languages — measured in
research/benchmarks/retrieval_calibration.py, and a bigger model does not fix it — so the
passage is bridged at ingest instead.

No model runs here. The translator is scripted, because what is under test is the wiring:
that copies are made, that they point at their origin, that a failure costs one language
and not the document, and that deleting the document takes the copies with it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from ars_gateway.documents import DocumentLibrary
from ars_memory import MemoryConfig, SqliteMemoryStore
from ars_protocol import Language

LEASE_DE = (
    "MIETVERTRAG — Republicii 42\n\n"
    "Die Miete beträgt monatlich 1.200 Euro und ist zum Dritten fällig.\n"
    "Die Kaution entspricht zwei Monatsmieten, also 2.400 Euro.\n"
    "Ein Stellplatz ist nicht enthalten. Er kostet 80 Euro zusätzlich pro Monat.\n"
)


class _ScriptedTranslator:
    """Returns a marked copy, and can be told to fail for one language."""

    def __init__(self, *, fail_for: Language | None = None) -> None:
        self.fail_for = fail_for
        self.calls: list[tuple[Language, Language]] = []

    async def translate_text(self, text: str, *, source: Language, target: Language) -> str:
        self.calls.append((source, target))
        if target is self.fail_for:
            raise RuntimeError("refused: output is 12% of the source length")
        return f"[{target.value}] {text}"


@pytest.fixture
def config(tmp_path: Path) -> MemoryConfig:
    return MemoryConfig(data_dir=tmp_path, embedding_backend="hash")


async def _settle(library: DocumentLibrary) -> None:
    """Ingest returns before translation finishes, on purpose. Tests wait for it."""
    for task in list(library._translating.values()):
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_german_document_is_indexed_in_english_and_romanian(config) -> None:
    store = await SqliteMemoryStore.open(config)
    try:
        translator = _ScriptedTranslator()
        library = DocumentLibrary(store, translator=translator)

        doc = await library.learn("Mietvertrag.txt", LEASE_DE.encode())
        await _settle(library)

        # One chunk, in German, plus a copy in each of the other two languages.
        assert {t for _s, t in translator.calls} == {Language.EN, Language.RO}
        records = [await store.get(i) for i in library._chunk_ids[doc.id]]
        by_language = {r.language for r in records if r}
        assert by_language == {Language.DE, Language.EN, Language.RO}

        copies = [r for r in records if r and r.language is not Language.DE]
        assert all(c.origin_id for c in copies), "a copy with no origin is a leak"
        assert all(c.provenance.label == records[0].provenance.label for c in copies), \
            "a translation must cite the file it came from, not itself"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_the_document_answers_in_its_own_language_before_translation_finishes(
    config,
) -> None:
    """The upload returns as soon as the file is learned. Waiting for translation would
    trade the thing the user asked for against the thing they did not."""
    store = await SqliteMemoryStore.open(config)
    try:
        library = DocumentLibrary(store, translator=_ScriptedTranslator())
        await library.learn("Mietvertrag.txt", LEASE_DE.encode())

        assert await store.recall("Miete") != (), "not answerable until translated"
        await _settle(library)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_one_failed_language_does_not_cost_the_others(config) -> None:
    store = await SqliteMemoryStore.open(config)
    try:
        library = DocumentLibrary(store, translator=_ScriptedTranslator(fail_for=Language.RO))

        doc = await library.learn("Mietvertrag.txt", LEASE_DE.encode())
        await _settle(library)

        languages = {r.language for i in library._chunk_ids[doc.id] if (r := await store.get(i))}
        assert languages == {Language.DE, Language.EN}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_forgetting_the_document_takes_every_translation(config) -> None:
    store = await SqliteMemoryStore.open(config)
    try:
        library = DocumentLibrary(store, translator=_ScriptedTranslator())
        doc = await library.learn("Mietvertrag.txt", LEASE_DE.encode())
        await _settle(library)

        await library.forget(doc.id)

        assert await store.recall("Miete") == ()
        assert await store.recall("[en]") == ()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_no_translator_means_no_copies(config) -> None:
    store = await SqliteMemoryStore.open(config)
    try:
        library = DocumentLibrary(store, translator=None)
        doc = await library.learn("Mietvertrag.txt", LEASE_DE.encode())
        await _settle(library)

        records = [await store.get(i) for i in library._chunk_ids[doc.id]]
        assert {r.language for r in records if r} == {Language.DE}
    finally:
        await store.close()
