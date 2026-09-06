"""Live latency tests against a real Ollama. Skipped when no server is listening.

Every other test in this directory runs against a scripted model, which is right for
asserting behaviour and useless for asserting speed. The `think` regression — 16.9 s to
first token on a 200 ms budget — passed the entire unit suite, because no unit test can
know how long a real model takes to start talking. This file is the answer to that.

Run it with a server up:

    ollama serve &
    ollama pull qwen3:14b
    uv run pytest tests/unit/compute/test_ollama_live.py -v

It is not part of CI on a machine without a GPU, and it should not be: the numbers are
hardware-specific. It is a gate for the reference deployment, and the skip message says
so out loud rather than passing silently.
"""

from __future__ import annotations

import json
import os
import statistics
import time
import urllib.error
import urllib.request

import pytest
from ars_compute import ReasoningMode
from ars_compute.backends.ollama import OllamaBackend
from ars_compute.context import ContextAssembler
from ars_compute.language import diacritics_report, resolve_reply_language
from ars_protocol import Language
from helpers import utterance

HOST = os.environ.get("ARS_LLM_LOCAL_HOST", "http://127.0.0.1:11434")
MODEL = os.environ.get("ARS_LLM_LOCAL_MODEL", "qwen3:14b")

# docs/architecture/overview.md, "LLM first token", p95. ADR 0001 revised ENDPOINTING and
# TOTAL; this row is unchanged.
FIRST_TOKEN_BUDGET_MS = 200


def _probe() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"{HOST}/api/tags", timeout=1.5) as r:  # noqa: S310
            models = {m.get("model") for m in json.load(r).get("models", [])}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"no Ollama at {HOST} ({type(exc).__name__})"
    if MODEL not in models:
        return False, f"{MODEL} not pulled (have: {sorted(models) or 'nothing'})"
    return True, ""


_LIVE, _WHY = _probe()
pytestmark = pytest.mark.skipif(not _LIVE, reason=_WHY)


async def _first_token_ms(backend: OllamaBackend, text: str, language: Language,
                          reasoning: ReasoningMode) -> tuple[float, str]:
    tr = utterance(text, language)
    ctx = ContextAssembler().assemble(
        turn_id="trn_" + "e" * 20, language=resolve_reply_language(tr), transcript=tr,
    )
    t0 = time.perf_counter()
    first = None
    reply = ""
    async for piece in await backend.complete_context(ctx, reasoning=reasoning):
        if isinstance(piece, str) and piece.strip() and first is None:
            first = (time.perf_counter() - t0) * 1000
        if isinstance(piece, str):
            reply += piece
    return (first if first is not None else float("inf")), reply


async def _server_busy_ms() -> float:
    """Time a minimal generation that bypasses everything this service does.

    Latency tests on a contended GPU measure the contention. Failing the build for that
    teaches people to ignore the gate, which is as bad as not having one. But *skipping*
    on slowness would also hide a real regression, so the probe has to distinguish the
    two: it sends its own one-token request with `think` set explicitly, so it depends on
    the server and not on any code under test. Slow probe = busy machine, skip. Fast probe
    plus slow turns = our problem, fail.
    """
    be = OllamaBackend(host=HOST, model=MODEL, num_predict=1)
    try:
        t0 = time.perf_counter()
        async for _ in await be.complete(system="Reply with one word.", context=(),
                                         language=Language.EN,
                                         reasoning=ReasoningMode.OFF):
            break
        return (time.perf_counter() - t0) * 1000
    finally:
        await be.aclose()


BUSY_THRESHOLD_MS = FIRST_TOKEN_BUDGET_MS * 4


@pytest.fixture
async def backend():
    be = OllamaBackend(host=HOST, model=MODEL)
    await be.capabilities()          # resolve thinking support before timing anything
    # Warm exactly the way a deployment should: weights loaded, and the system-prompt
    # prefix prefilled for BOTH languages. Model load is a keep_alive concern rather than
    # part of the first-token budget. The per-language part is not an optimisation: an
    # unwarmed Romanian prefix measured 1158 ms to first token against 31 ms warm, and
    # this suite is what found that.
    assembler = ContextAssembler()
    await be.warm([assembler.build_system(lang)[0] for lang in (Language.EN, Language.RO)])

    busy = await _server_busy_ms()
    if busy > BUSY_THRESHOLD_MS:
        await be.aclose()
        pytest.skip(
            f"server is contended: a bare one-token generation took {busy:.0f} ms "
            f"(> {BUSY_THRESHOLD_MS} ms). Latency here would measure the load, not the "
            f"code. Re-run on an idle machine."
        )
    yield be
    await be.aclose()


@pytest.mark.parametrize("language,text", [
    (Language.EN, "Tell me in two sentences what you can do."),
    (Language.RO, "Spune-mi în două propoziții ce poți face."),
])
async def test_first_token_meets_the_budget_on_a_spoken_turn(backend, language, text):
    """The regression gate. A spoken turn uses ReasoningMode.OFF, and that has to be fast
    enough that the voice pipeline still adds up."""
    samples = []
    for _ in range(5):
        ms, reply = await _first_token_ms(backend, text, language, ReasoningMode.OFF)
        samples.append(ms)
        assert reply.strip(), "empty reply — check num_predict against the thinking budget"

    # Asserted on the BEST sample, which needs justifying because it looks like cheating.
    #
    # This is a gate, not a benchmark. The published p50/p95 come from
    # `research/benchmarks/compute/latency_live.py` on an idle machine, which is where
    # percentile numbers belong. What a gate has to answer is narrower: "can this
    # configuration meet the budget on this hardware at all?"
    #
    # The two failure modes are shaped completely differently. Contention on a shared GPU
    # produces a few multi-second outliers among good samples — observed
    # [161, 907, 4123, 177, 259] with an unrelated job running — and says nothing about
    # this code. The regression this test exists for moves *every* sample by ~30x
    # (111 ms -> 4221 ms), because thinking happens on every request. So the minimum
    # separates them cleanly: it stays under budget under any amount of load, and it is
    # ~4000 ms the moment `think` stops being sent. A median would flake on the first and
    # a max would flake constantly.
    listed = [round(s) for s in samples]
    best = min(samples)
    assert best <= FIRST_TOKEN_BUDGET_MS, (
        f"{language.value}: best of {len(samples)} first-token samples was {best:.0f} ms, "
        f"over the {FIRST_TOKEN_BUDGET_MS} ms budget (all: {listed}, median "
        f"{statistics.median(samples):.0f} ms). If `think` stopped being sent, this is "
        f"why — an omitted `think` means the model thinks, on every single request."
    )


async def test_thinking_is_actually_slower_so_the_policy_is_not_cargo_cult(backend):
    """If this ever fails, the reason for the whole policy has gone away and it should be
    deleted rather than carried."""
    fast, _ = await _first_token_ms(
        backend, "Tell me in two sentences what you can do.", Language.EN, ReasoningMode.OFF)
    slow, _ = await _first_token_ms(
        backend, "Tell me in two sentences what you can do.", Language.EN, ReasoningMode.ON)
    assert slow > fast * 5, f"off={fast:.0f}ms on={slow:.0f}ms"


async def test_thinking_output_never_reaches_the_reply(backend):
    """`message.thinking` is a sibling of `message.content`. Streaming it into ReplyDelta
    would read the model's private reasoning aloud through TTS."""
    _, reply = await _first_token_ms(
        backend, "What is 17 times 23? Answer with just the number.", Language.EN,
        ReasoningMode.ON)
    assert reply.strip()
    stats = backend.last_stats
    assert stats is not None
    assert stats.extra.get("thinking_chars", 0) > 0, "expected the model to think here"
    # The thinking was counted, and none of it was emitted as reply text.
    assert "</think>" not in reply and "<think>" not in reply


async def test_romanian_still_comes_back_with_diacritics_without_thinking(backend):
    """Turning thinking off is a latency change, not a quality one. If Romanian degrades
    when the model stops thinking, the trade is not the one we think we are making."""
    _, reply = await _first_token_ms(
        backend, "Spune-mi în două propoziții ce poți face.", Language.RO,
        ReasoningMode.OFF)
    report = diacritics_report(reply, Language.RO)
    assert not report.repairable, f"{reply!r} -> {report.repairable}"
    assert report.diacritic_chars > 0, f"no Romanian diacritics at all in {reply!r}"


async def test_warming_covers_both_languages(backend):
    """A.R.S has a system prompt per language, so it has a prefix per language, so it has
    a cold start per language. Warming one and calling it done leaves the first Romanian
    utterance after every restart on the slow path."""
    assembler = ContextAssembler()
    systems = [assembler.build_system(lang)[0] for lang in (Language.EN, Language.RO)]
    timings = await backend.warm(systems)
    assert len(timings) == 2, timings


async def test_romanian_is_not_disproportionately_slow(backend):
    """Regression for the cold-prefix cliff this suite found. With both prefixes warm, a
    Romanian turn costs about what an English one does, despite a 34% longer prompt."""
    en, _ = await _first_token_ms(backend, "Say something short.", Language.EN,
                                  ReasoningMode.OFF)
    ro, _ = await _first_token_ms(backend, "Zi-mi ceva scurt.", Language.RO,
                                  ReasoningMode.OFF)
    assert ro <= FIRST_TOKEN_BUDGET_MS, f"ro={ro:.0f}ms en={en:.0f}ms"
    assert ro < en * 4, f"Romanian disproportionately slow: ro={ro:.0f}ms en={en:.0f}ms"


async def test_the_server_reports_thinking_as_a_capability(backend):
    caps = await backend.capabilities()
    assert "thinking" in caps, f"{MODEL} capabilities: {sorted(caps)}"
    assert backend._thinking_supported is True


def test_the_skip_reason_is_visible_when_there_is_no_server():
    """Meta-test: a live suite that skips silently is a live suite nobody notices is dead."""
    assert _LIVE or _WHY, "skip must always carry a reason"
