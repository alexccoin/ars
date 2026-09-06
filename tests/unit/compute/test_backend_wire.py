"""Wire-format tests for the two real backends.

Neither is exercised against a live server here. Ollama is not installed on this machine,
and the Anthropic API costs money and is non-deterministic. What IS testable — and what
actually breaks in production — is the translation in and out: message shape, tool schema,
SSE/NDJSON parsing, partial-JSON accumulation, and cancellation. All of that is driven
through `httpx.MockTransport` against recorded response bodies taken from the published
documentation.
"""

from __future__ import annotations

import json

import httpx
import pytest
from ars_compute.backends.anthropic import API_VERSION, DEFAULT_MODEL, AnthropicBackend
from ars_compute.backends.base import Message
from ars_compute.backends.ollama import OllamaBackend, parse_chunk, to_wire
from ars_compute.context import Role
from ars_compute.errors import BackendUnavailable
from ars_compute.toolschema import to_anthropic, to_ollama
from ars_protocol import Language, ToolCall
from helpers import EMAIL_SEND, WEB_FETCH


# =============================================================== ollama
def test_ollama_parses_a_text_delta() -> None:
    text, calls, done, _ = parse_chunk(json.dumps(
        {"model": "qwen3:14b", "message": {"role": "assistant", "content": "He"}, "done": False}
    ))
    assert text == "He" and not calls and not done


def test_ollama_parses_a_tool_call_with_already_parsed_arguments() -> None:
    _, calls, _, _ = parse_chunk(json.dumps({
        "message": {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "web_fetch",
                                                 "arguments": {"url": "https://x/y"}}}]},
        "done": False,
    }))
    assert len(calls) == 1
    assert calls[0].tool == "web_fetch" and calls[0].arguments == {"url": "https://x/y"}


def test_ollama_tolerates_arguments_delivered_as_a_json_string() -> None:
    _, calls, _, _ = parse_chunk(json.dumps({
        "message": {"tool_calls": [{"function": {"name": "web_fetch",
                                                 "arguments": '{"url": "https://x/y"}'}}]},
        "done": False,
    }))
    assert calls[0].arguments == {"url": "https://x/y"}


def test_ollama_reads_the_final_metrics() -> None:
    _, _, done, metrics = parse_chunk(json.dumps({
        "message": {"content": ""}, "done": True, "done_reason": "stop",
        "prompt_eval_count": 512, "eval_count": 64, "total_duration": 1_200_000_000,
    }))
    assert done and metrics["prompt_eval_count"] == 512 and metrics["eval_count"] == 64


def test_ollama_skips_a_truncated_line_rather_than_failing_the_turn() -> None:
    assert parse_chunk('{"message": {"con') == ("", [], False, {})
    assert parse_chunk("") == ("", [], False, {})


def test_ollama_surfaces_a_server_error() -> None:
    with pytest.raises(BackendUnavailable):
        parse_chunk(json.dumps({"error": "model 'qwen3:14b' not found"}))


def test_ollama_message_shape() -> None:
    wire = to_wire("SYSTEM", [
        Message(role=Role.USER, text="hello"),
        Message(role=Role.ASSISTANT, tool_call=ToolCall(id="tc_1", tool="web_fetch",
                                                        arguments={"url": "https://x"})),
        Message(role=Role.USER, text="[EXTERNAL DATA] page body", tool_result_for="tc_1"),
    ])
    assert wire[0] == {"role": "system", "content": "SYSTEM"}
    assert wire[1] == {"role": "user", "content": "hello"}
    assert wire[2]["tool_calls"][0]["function"]["name"] == "web_fetch"
    assert wire[3]["role"] == "tool"
    assert wire[3]["tool_name"] == "tc_1"
    assert "[EXTERNAL DATA]" in wire[3]["content"], "the quarantine frame must survive"


def test_ollama_payload_holds_the_context_window_and_a_fence_stop() -> None:
    b = OllamaBackend(model="qwen3:14b", num_ctx=8192)
    body = b.payload("SYSTEM", [Message(role=Role.USER, text="hi")], (WEB_FETCH,))
    assert body["stream"] is True
    assert body["options"]["num_ctx"] == 8192
    assert "<<<ARS-EXTERNAL-" in body["options"]["stop"]
    assert body["tools"][0]["function"]["name"] == "web_fetch"


async def test_ollama_streams_text_then_a_tool_call() -> None:
    lines = [
        json.dumps({"message": {"content": "Let me "}, "done": False}),
        json.dumps({"message": {"content": "check. "}, "done": False}),
        json.dumps({"message": {"tool_calls": [
            {"function": {"name": "web_fetch", "arguments": {"url": "https://x"}}}]},
            "done": False}),
        json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop",
                    "prompt_eval_count": 100, "eval_count": 12}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(200, content="\n".join(lines).encode())

    b = OllamaBackend(client=httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                               base_url="http://x"))
    out = [p async for p in await b.complete(system="S", context=(), language=Language.EN)]
    assert "".join(p for p in out if isinstance(p, str)) == "Let me check. "
    assert isinstance(out[-1], ToolCall) and out[-1].tool == "web_fetch"
    assert b.last_stats.input_tokens == 100 and b.last_stats.output_tokens == 12
    assert b.last_stats.first_token_ms is not None


async def test_ollama_connection_refused_is_a_clear_message_not_a_traceback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    b = OllamaBackend(client=httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                               base_url="http://x"))
    with pytest.raises(BackendUnavailable, match="ollama serve"):
        [p async for p in await b.complete(system="S", context=(), language=Language.EN)]


async def test_ollama_cancellation_stops_consuming_the_stream() -> None:
    lines = [json.dumps({"message": {"content": f"tok{i} "}, "done": False}) for i in range(500)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content="\n".join(lines).encode())

    b = OllamaBackend(client=httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                               base_url="http://x"))
    seen = 0
    async for _ in await b.complete(system="S", context=(), language=Language.EN):
        seen += 1
        if seen == 5:
            await b.cancel()
    assert seen < 500
    assert b.last_stats.cancelled


# =============================================================== anthropic
def sse(*events: tuple[str, dict]) -> bytes:
    return "".join(
        f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events
    ).encode()


TOOL_STREAM = sse(
    ("message_start", {"type": "message_start",
                       "message": {"usage": {"input_tokens": 472, "output_tokens": 0}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": "Okay, let me check"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("content_block_start", {"type": "content_block_start", "index": 1,
                             "content_block": {"type": "tool_use",
                                               "id": "toolu_01T1x1fJ34qAmk2tNTrN7Up6",
                                               "name": "web_fetch", "input": {}}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 1,
                             "delta": {"type": "input_json_delta", "partial_json": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 1,
                             "delta": {"type": "input_json_delta",
                                       "partial_json": '{"url":'}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 1,
                             "delta": {"type": "input_json_delta",
                                       "partial_json": ' "https://exa'}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 1,
                             "delta": {"type": "input_json_delta",
                                       "partial_json": 'mple.com"}'}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 1}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                       "usage": {"output_tokens": 89}}),
    ("message_stop", {"type": "message_stop"}),
)


def anthropic_backend(body: bytes, *, capture: dict | None = None) -> AnthropicBackend:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["headers"] = dict(request.headers)
            capture["json"] = json.loads(request.content)
            capture["path"] = request.url.path
        return httpx.Response(200, content=body)

    return AnthropicBackend(
        api_key="test-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://api.anthropic.com"),
    )


async def test_anthropic_accumulates_partial_json_into_one_tool_call() -> None:
    capture: dict = {}
    b = anthropic_backend(TOOL_STREAM, capture=capture)
    out = [p async for p in await b.complete(system="S", context=(), tools=(WEB_FETCH,),
                                             language=Language.EN)]
    text = "".join(p for p in out if isinstance(p, str))
    calls = [p for p in out if isinstance(p, ToolCall)]
    assert text == "Okay, let me check"
    assert len(calls) == 1
    assert calls[0].tool == "web_fetch"
    assert calls[0].arguments == {"url": "https://example.com"}
    assert b.last_stats.input_tokens == 472 and b.last_stats.output_tokens == 89
    assert b.last_stats.extra["stop_reason"] == "tool_use"


async def test_anthropic_request_shape() -> None:
    capture: dict = {}
    b = anthropic_backend(TOOL_STREAM, capture=capture)
    [_ async for _ in await b.complete(system="SYS", context=(), tools=(WEB_FETCH,),
                                       language=Language.EN)]
    body, headers = capture["json"], capture["headers"]
    assert capture["path"] == "/v1/messages"
    assert headers["anthropic-version"] == API_VERSION
    assert headers["x-api-key"] == "test-key"
    assert body["model"] == DEFAULT_MODEL == "claude-sonnet-5"
    assert body["stream"] is True and body["system"] == "SYS"
    assert body["max_tokens"] > 0
    # latency decision, documented in the backend module
    assert body["output_config"] == {"effort": "low"}
    # one tool at a time: a parallel batch cannot be shown as a single consent prompt
    assert body["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert body["tools"][0]["name"] == "web_fetch"
    assert body["tools"][0]["input_schema"]["properties"]["url"]["format"] == "uri"


async def test_anthropic_thinking_is_consumed_and_never_spoken() -> None:
    stream = sse(
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "thinking_delta",
                                           "thinking": "The user probably means..."}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "signature_delta", "signature": "Eq=="}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {"type": "content_block_start", "index": 1,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                 "delta": {"type": "text_delta", "text": "Tomorrow at nine."}}),
        ("message_stop", {"type": "message_stop"}),
    )
    b = anthropic_backend(stream)
    out = [p async for p in await b.complete(system="S", context=(), language=Language.EN)]
    assert "".join(p for p in out if isinstance(p, str)) == "Tomorrow at nine."


async def test_anthropic_ignores_unknown_event_types() -> None:
    stream = sse(
        ("ping", {"type": "ping"}),
        ("some_future_event", {"type": "some_future_event", "whatever": 1}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "fine"}}),
        ("message_stop", {"type": "message_stop"}),
    )
    b = anthropic_backend(stream)
    out = [p async for p in await b.complete(system="S", context=(), language=Language.EN)]
    assert "".join(p for p in out if isinstance(p, str)) == "fine"


async def test_anthropic_error_event_becomes_backend_unavailable() -> None:
    stream = sse(("error", {"type": "error", "error": {"type": "overloaded_error",
                                                       "message": "Overloaded"}}))
    b = anthropic_backend(stream)
    with pytest.raises(BackendUnavailable, match="overloaded_error"):
        [p async for p in await b.complete(system="S", context=(), language=Language.EN)]


async def test_anthropic_without_a_key_fails_before_any_network_call() -> None:
    b = AnthropicBackend(api_key="")
    assert not b.configured
    with pytest.raises(BackendUnavailable, match="ANTHROPIC_API_KEY"):
        [p async for p in await b.complete(system="S", context=(), language=Language.EN)]


async def test_anthropic_tool_result_id_round_trips_to_the_provider_id() -> None:
    """Our audit uses `tc_…`; the API needs its own `toolu_…` back. Both must be true at
    once or the follow-up request 400s."""
    b = anthropic_backend(TOOL_STREAM)
    out = [p async for p in await b.complete(system="S", context=(), tools=(WEB_FETCH,),
                                             language=Language.EN)]
    call = next(p for p in out if isinstance(p, ToolCall))
    assert call.id.startswith("tc_")
    wire = b.to_wire([
        Message(role=Role.ASSISTANT, tool_call=call),
        Message(role=Role.USER, text="[EXTERNAL DATA] body", tool_result_for=call.id),
    ])
    assert wire[0]["content"][0]["id"] == "toolu_01T1x1fJ34qAmk2tNTrN7Up6"
    assert wire[1]["content"][0]["tool_use_id"] == "toolu_01T1x1fJ34qAmk2tNTrN7Up6"
    assert "[EXTERNAL DATA]" in wire[1]["content"][0]["content"]


def test_anthropic_runs_locally_is_false() -> None:
    assert AnthropicBackend(api_key="x").runs_locally is False
    assert OllamaBackend().runs_locally is True


# =============================================================== tool schemas
def test_tool_schema_warns_the_model_that_a_tool_returns_hostile_text() -> None:
    desc = to_anthropic((WEB_FETCH,))[0]["description"]
    assert "written by someone other than the user" in desc
    assert "taints the turn" in desc
    assert "web.fetch" in desc
    plain = to_anthropic((EMAIL_SEND,))[0]["description"]
    assert "written by someone other than the user" not in plain


def test_tool_schema_types_come_from_the_versioned_map() -> None:
    schema = to_ollama((EMAIL_SEND,))[0]["function"]["parameters"]
    assert schema["properties"]["to"] == {"type": "string", "format": "email",
                                          "description": "Recipient."}
    assert schema["required"] == ["to", "body"]
    assert schema["additionalProperties"] is False


def test_an_unmapped_param_type_is_reported_not_silently_coerced() -> None:
    from ars_compute.toolschema import input_schema, unmapped_types
    from ars_protocol import ToolParam, ToolSpec
    spec = ToolSpec(name="weird_tool", description="d",
                    params=(ToolParam(name="p", type="duration", description="d"),))
    assert input_schema(spec)["properties"]["p"]["type"] == "string"
    assert "duration" in unmapped_types()
