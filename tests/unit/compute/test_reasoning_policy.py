"""Thinking: when it is bought, and how it reaches the wire.

This exists because of a real regression. `OllamaBackend` shipped without sending `think`
at all, which for a reasoning model means "think as much as you like": measured 6.5 s to
first token cold and 16.9 s warm, against a 200 ms budget. Every test in this repo passed,
because a `ScriptedBackend` has no opinion about how long a real model takes to start
talking. So these tests assert the *flag*, and `test_ollama_live.py` asserts the *latency*.
"""

from __future__ import annotations

import pytest
from ars_compute import ReasoningMode, Scene, ScriptedBackend, ThinkPolicy, TurnOrchestrator
from ars_compute.backends.base import Message
from ars_compute.backends.ollama import OllamaBackend, _rejected_think
from ars_compute.backends.router import Router, RoutingPolicy
from ars_compute.context import Role
from ars_protocol import Language
from helpers import EXTERNAL_PROV, WEB_FETCH, FakeGuard, FakeSkills, utterance

MSGS = [Message(role=Role.USER, text="hi")]


# ---------------------------------------------------------------- the wire
def test_a_spoken_turn_sends_think_false() -> None:
    """The whole regression, in one assertion. `think` absent is NOT equivalent to
    `think: false` — absent means the model decides, and it decides to think."""
    body = OllamaBackend().payload("S", MSGS, (), ReasoningMode.OFF)
    assert body["think"] is False


def test_the_default_payload_is_the_fast_one() -> None:
    """A caller who passes no reasoning at all must not silently get the 17-second path."""
    body = OllamaBackend().payload("S", MSGS, ())
    assert body["think"] is False


@pytest.mark.parametrize("mode,expected", [
    (ReasoningMode.OFF, False),
    (ReasoningMode.ON, True),
    (ReasoningMode.LOW, "low"),
    (ReasoningMode.MEDIUM, "medium"),
    (ReasoningMode.HIGH, "high"),
    (ReasoningMode.MAX, "max"),
])
def test_every_mode_maps_to_a_value_ollama_accepts(mode: ReasoningMode, expected) -> None:
    # The set Ollama itself names in its 400: 'must be "high", "medium", "low", "max",
    # true, or false'.
    assert OllamaBackend().payload("S", MSGS, (), mode)["think"] == expected


def test_provider_default_omits_the_field_entirely() -> None:
    body = OllamaBackend().payload("S", MSGS, (), ReasoningMode.PROVIDER_DEFAULT)
    assert "think" not in body


def test_thinking_gets_a_bigger_output_allowance() -> None:
    """Ollama spends thinking tokens from `num_predict`. Measured: think=true with
    num_predict=120 returned an empty reply and done_reason=length."""
    off = OllamaBackend().payload("S", MSGS, (), ReasoningMode.OFF)
    on = OllamaBackend().payload("S", MSGS, (), ReasoningMode.ON)
    assert on["options"]["num_predict"] > off["options"]["num_predict"]


def test_a_model_known_not_to_think_gets_no_think_field() -> None:
    b = OllamaBackend(model="some-plain-model")
    b._thinking_supported = False
    assert "think" not in b.payload("S", MSGS, (), ReasoningMode.OFF)


@pytest.mark.parametrize("status,detail,expected", [
    (400, 'invalid think value: "banana"', True),
    (400, "model does not support thinking", True),
    (404, "model 'x' not found", False),
    (500, "internal error", False),
    (400, "invalid options.num_ctx", False),
])
def test_only_a_think_complaint_triggers_the_fallback(status, detail, expected) -> None:
    assert _rejected_think(status, detail) is expected


def test_anthropic_maps_the_same_policy_onto_effort() -> None:
    """One knob, two providers. A spoken turn is cheap on both or the policy is a lie."""
    from ars_compute.backends.anthropic import AnthropicBackend
    b = AnthropicBackend(api_key="k")
    assert b.payload("S", MSGS, (), ReasoningMode.OFF)["output_config"] == {"effort": "low"}
    assert b.payload("S", MSGS, (), ReasoningMode.ON)["output_config"] == {"effort": "high"}
    assert b.payload("S", MSGS, (), ReasoningMode.MAX)["output_config"] == {"effort": "max"}


# ---------------------------------------------------------------- the policy
def test_spoken_turns_never_think() -> None:
    p = ThinkPolicy()
    assert p.decide(spoken=True) is ReasoningMode.OFF
    assert p.decide(spoken=True, escalated=True) is ReasoningMode.OFF
    assert p.decide(spoken=True, round_index=3) is ReasoningMode.OFF


def test_typed_turns_may_think() -> None:
    assert ThinkPolicy().decide(spoken=False) is ReasoningMode.ON


def test_the_policy_is_configurable_without_touching_the_backend() -> None:
    """A deployment on a model whose intermediate levels work should not need a code
    change to use them."""
    p = ThinkPolicy(spoken=ReasoningMode.LOW, typed=ReasoningMode.MAX)
    assert p.decide(spoken=True) is ReasoningMode.LOW
    assert p.decide(spoken=False) is ReasoningMode.MAX


# ---------------------------------------------------------------- end to end
async def test_the_orchestrator_asks_for_no_thinking_on_a_spoken_turn(session) -> None:
    backend = ScriptedBackend([Scene(name="s", text="Nothing today.")])
    orch = TurnOrchestrator(backend=backend, guard=FakeGuard(), skills=FakeSkills())
    async for _ in orch.run(session=session, transcript=utterance("What's on today?"),
                            spoken=True):
        pass
    assert backend.last.reasoning is ReasoningMode.OFF
    assert orch.last.stats.reasoning == "off"


async def test_the_orchestrator_allows_thinking_on_a_typed_turn(session) -> None:
    backend = ScriptedBackend([Scene(name="s", text="Nothing today.")])
    orch = TurnOrchestrator(backend=backend, guard=FakeGuard(), skills=FakeSkills())
    async for _ in orch.run(session=session, transcript=utterance("What's on today?"),
                            spoken=False):
        pass
    assert backend.last.reasoning is ReasoningMode.ON
    assert orch.last.stats.reasoning == "on"


async def test_every_round_of_a_spoken_tool_turn_stays_fast(session) -> None:
    """The round after a tool result is still a user waiting for an answer."""
    backend = ScriptedBackend([
        Scene(name="fetch", at_call=0, tool=("web_fetch", {"url": "https://e/x"})),
        Scene(name="answer", at_call=1, text="It says hello."),
    ])
    skills = FakeSkills(content=(("hello", EXTERNAL_PROV),))
    orch = TurnOrchestrator(backend=backend, guard=FakeGuard(), skills=skills)
    async for _ in orch.run(session=session, transcript=utterance("Read that page."),
                            tools=(WEB_FETCH,), spoken=True):
        pass
    assert [i.reasoning for i in backend.invocations] == [ReasoningMode.OFF] * 2


async def test_the_router_passes_reasoning_through_to_the_chosen_backend(session) -> None:
    local = ScriptedBackend([Scene(name="s", text="hi")], name="local", runs_locally=True)
    router = Router(local=local, policy=RoutingPolicy.LOCAL_ONLY)
    orch = TurnOrchestrator(backend=router, guard=FakeGuard(), skills=FakeSkills())
    async for _ in orch.run(session=session, transcript=utterance("Salut, ce faci?",
                                                                 Language.RO), spoken=True):
        pass
    assert local.last.reasoning is ReasoningMode.OFF


# ---------------------------------------------------------------- call ergonomics
async def test_complete_works_both_with_and_without_await() -> None:
    """`LlmBackend.complete` is declared `async def -> AsyncIterator`, so the contract call
    is `async for x in await be.complete(...)`. That double keyword catches everyone out,
    so backends here accept both spellings."""
    backend = ScriptedBackend([Scene(name="s", text="hello there")])

    awaited = [p async for p in await backend.complete(
        system="S", context=(), language=Language.EN)]
    direct = [p async for p in backend.complete(
        system="S", context=(), language=Language.EN)]
    assert "".join(awaited) == "".join(direct) == "hello there"


async def test_stream_reply_works_against_a_bare_llmbackend() -> None:
    """The helper has to cope with a third-party backend that follows the literal
    interface and never heard of `reasoning`."""
    from ars_compute import stream_reply

    class Bare:
        runs_locally = True

        async def complete(self, *, system, context, tools=(), language):
            async def gen():
                yield "plain "
                yield "backend"
            return gen()

        async def cancel(self) -> None: ...

    out = [p async for p in stream_reply(Bare(), system="S", context=(),
                                         language=Language.EN,
                                         reasoning=ReasoningMode.OFF)]
    assert "".join(out) == "plain backend"


def test_escalation_cannot_make_a_spoken_turn_slow() -> None:
    """`spoken_after_filler` is the only knob that lets a spoken turn think. Escalation
    means the question is hard, not that the user stopped waiting for a voice."""
    p = ThinkPolicy()
    assert p.decide(spoken=True, escalated=True, round_index=2,
                    filler_spoken=True) is ReasoningMode.OFF


def test_the_one_knob_that_lets_a_spoken_turn_think() -> None:
    p = ThinkPolicy(spoken_after_filler=ReasoningMode.ON)
    assert p.decide(spoken=True) is ReasoningMode.OFF
    assert p.decide(spoken=True, round_index=1, filler_spoken=False) is ReasoningMode.OFF
    assert p.decide(spoken=True, round_index=1, filler_spoken=True) is ReasoningMode.ON
