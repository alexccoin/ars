"""Local reasoning through Ollama's HTTP API. The reference deployment.

This is the only module in A.R.S that knows Ollama's wire format. It speaks
`POST /api/chat` with `stream: true`, which answers newline-delimited JSON objects:

    {"model":"qwen3:14b","created_at":"...","message":{"role":"assistant","content":"He"},
     "done":false}
    {"model":"qwen3:14b","message":{"role":"assistant","content":"","tool_calls":[
       {"function":{"name":"web_search","arguments":{"q":"..."}}}]},"done":false}
    {"model":"qwen3:14b","message":{"role":"assistant","content":""},"done":true,
     "done_reason":"stop","total_duration":...,"prompt_eval_count":...,"eval_count":...}

Two differences from the Anthropic path are worth knowing before reading the code:

  * Ollama delivers tool-call **arguments already parsed** as a JSON object, in one
    chunk. There is no partial-JSON accumulation to do, unlike the Messages API.
  * Ollama does not give tool calls an id. `ToolCall` in the protocol generates its own,
    and the tool result is fed back with `role: "tool"` plus `tool_name`, which is how
    Ollama's chat templates match a result to a call.

**Thinking is off by default, and that is a latency decision with a measurement behind
it.** `qwen3:14b` is a reasoning model: unless told otherwise it emits a thinking block
before any visible content, so the first token the user hears arrives after the entire
reasoning pass. Measured warm on an M5 Max: 29 ms to first visible token with
`"think": false`, ~2.8 s with it on, ~17 s with the field omitted entirely. The budget is
200 ms. See `ars_compute.reasoning` for the full table and `ThinkPolicy` for when a turn
is allowed to buy thinking anyway.

Cancellation matters more here than anywhere else in the codebase. Barge-in has to *stop
generation*, not stop listening: a 14B model on a laptop that keeps decoding after the
user interrupts is holding the GPU that the next turn needs. Closing the streaming
response terminates the HTTP request, and Ollama aborts the running generation when its
client disconnects. That is why this file owns the transport rather than delegating to a
client library whose cancellation semantics we would have to trust.

Ollama is NOT installed on the development machine this was written on. This module is exercised by
`tests/unit/compute/test_backend_wire.py` over recorded response bodies, and by
`tests/unit/compute/test_ollama_live.py`, which runs against a real server when one is
listening on 127.0.0.1:11434 and skips otherwise. The live test is the only kind that
catches a latency cliff, because a scripted backend has no opinion about how long a real
model takes to start speaking.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
from ars_protocol import Language, ToolCall, ToolSpec

from ..context import Role
from ..errors import BackendUnavailable
from ..reasoning import ReasoningMode
from ..tokens import FREE
from ..toolschema import to_ollama
from .base import BackendInfo, BaseBackend, Message, StreamStats

_ROLE = {Role.SYSTEM: "system", Role.USER: "user", Role.ASSISTANT: "assistant"}


def to_wire(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    """Neutral messages -> Ollama's `messages` array.

    A tool result becomes `role: "tool"`. Its content is the quarantined rendering, fence
    and all — the frame travels with the data into the chat template, which is the whole
    point of computing it in `context.py` rather than in the prompt string.
    """
    wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m.tool_call is not None:
            wire.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "function": {"name": m.tool_call.tool, "arguments": m.tool_call.arguments}
                }],
            })
        elif m.tool_result_for is not None:
            wire.append({"role": "tool", "content": m.text, "tool_name": m.tool_result_for})
        else:
            wire.append({"role": _ROLE[m.role], "content": m.text})
    return wire


def parse_chunk(line: str) -> tuple[str, list[ToolCall], bool, dict[str, Any]]:
    """One NDJSON line -> (visible text delta, tool calls, done, metrics).

    A reasoning model puts its chain of thought in `message.thinking`, a sibling of
    `message.content`. Only `content` is returned here. Thinking is not the answer, and
    streaming it into `ReplyDelta` would put the model's private reasoning through TTS and
    read it aloud to the user. The thinking length is counted into the metrics instead, so
    a turn that spent its whole output allowance on reasoning is visible in telemetry.

    Tolerant by design: a malformed line is skipped rather than killing the turn, because
    a local server under memory pressure occasionally truncates a write and the user
    should get a slightly short answer instead of an error.
    """
    line = line.strip()
    if not line:
        return "", [], False, {}
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return "", [], False, {}
    if "error" in obj:
        raise BackendUnavailable("ollama", str(obj["error"]))

    msg = obj.get("message") or {}
    text = msg.get("content") or ""
    thinking = msg.get("thinking") or ""
    calls: list[ToolCall] = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            # Some builds hand back a JSON string rather than an object.
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"_raw": args}
        calls.append(ToolCall(tool=fn.get("name", ""), arguments=dict(args or {})))

    done = bool(obj.get("done"))
    metrics: dict[str, Any] = {"thinking_chars": len(thinking)} if thinking else {}
    metrics |= {
        k: obj[k] for k in
        ("done_reason", "total_duration", "load_duration", "prompt_eval_count",
         "prompt_eval_duration", "eval_count", "eval_duration")
        if k in obj
    } if done else {}
    return text, calls, done, metrics


class OllamaBackend(BaseBackend):
    """`runs_locally=True`. Nothing sent here leaves the machine."""

    def __init__(
        self,
        *,
        host: str = "http://127.0.0.1:11434",
        model: str = "qwen3:14b",
        num_ctx: int = 8192,
        temperature: float = 0.3,
        num_predict: int = 768,
        num_predict_thinking: int = 3072,
        keep_alive: str = "10m",
        connect_timeout_s: float = 2.0,
        read_timeout_s: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__()
        self._host = host.rstrip("/")
        self._num_ctx = num_ctx
        self._temperature = temperature
        self._num_predict = num_predict
        self._num_predict_thinking = num_predict_thinking
        self._keep_alive = keep_alive
        self._thinking_supported: bool | None = None
        """Tri-state. None = not yet known, and we optimistically send the field anyway;
        a 400 downgrades it to False for the rest of the process. Probing up front would
        add a round trip to the first turn of every session for a question that almost
        always has the same answer."""
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._host,
            timeout=httpx.Timeout(read_timeout_s, connect=connect_timeout_s),
        )
        self.last_stats: StreamStats | None = None
        self.info = BackendInfo(
            name="ollama", model=model, runs_locally=True, price=FREE,
            context_window=num_ctx, supports_tools=True,
            notes=(
                "reference deployment. Cost is zero and stays zero; the budgeted price is "
                "latency and the GPU. num_ctx is held at 8192 because KV cache growth is "
                "the dominant term in first-token latency on an M-series laptop. Thinking "
                "is off unless a turn buys it: measured 29 ms to first token with "
                "think=false against ~2.8 s with it on, on a 200 ms budget."
            ),
        )

    # ---------------------------------------------------------------- health
    async def available(self) -> bool:
        """Cheap liveness probe used by the router. Never on the hot path of a turn that
        is already committed to a backend — the router calls it before choosing."""
        try:
            r = await self._client.get("/api/tags", timeout=httpx.Timeout(1.5, connect=0.75))
            return r.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    async def capabilities(self) -> frozenset[str]:
        """What this model can do, per the server. `/api/show` returns e.g.
        `["completion", "tools", "thinking"]`.

        Worth calling once at startup: it resolves `_thinking_supported` before any user is
        waiting, which removes the one-off 400-and-retry from the first real turn.
        """
        try:
            r = await self._client.post("/api/show", json={"model": self.info.model},
                                        timeout=httpx.Timeout(3.0, connect=1.0))
            r.raise_for_status()
            caps = frozenset(r.json().get("capabilities") or ())
        except (httpx.HTTPError, OSError, ValueError):
            return frozenset()
        if caps:
            self._thinking_supported = "thinking" in caps
        return caps

    async def warm(self, systems: Sequence[str]) -> dict[str, float]:
        """Prefill the KV cache for each system prompt. Call once at startup.

        Measured: with the weights already loaded, the *first* turn in a given language
        still costs ~1.1 s to first token, while every later turn costs ~120 ms. The
        difference is the system-prompt prefix — A.R.S has one per language (884 tokens EN,
        1184 RO) and the first request in each has to prefill it.

        A bilingual household hits that on the first Romanian utterance after a restart,
        which is exactly the moment the assistant is being judged. Warming both prefixes
        costs two one-token generations at startup and removes it. Alternating between the
        two afterwards is free — measured at 116-139 ms either way, so the server keeps
        both prefixes, and this is a one-off cost rather than a per-switch one.

        Returns per-prompt warm durations for the startup log. Failures are swallowed:
        warming is an optimisation, and a cold cache must never stop A.R.S booting.
        """
        timings: dict[str, float] = {}
        for system in systems:
            t0 = asyncio.get_running_loop().time()
            body = self.payload(system, [Message(role=Role.USER, text=".")], ())
            body["options"] = {**body["options"], "num_predict": 1}
            body["stream"] = False
            try:
                r = await self._client.post("/api/chat", json=body,
                                            timeout=httpx.Timeout(120.0, connect=2.0))
                r.raise_for_status()
            except (httpx.HTTPError, OSError):
                continue
            timings[system[:24]] = (asyncio.get_running_loop().time() - t0) * 1000
        return timings

    async def model_present(self) -> bool:
        try:
            r = await self._client.get("/api/tags", timeout=httpx.Timeout(1.5, connect=0.75))
            r.raise_for_status()
            names = {m.get("name", "") for m in r.json().get("models", [])}
            return self.info.model in names or f"{self.info.model}:latest" in names
        except (httpx.HTTPError, OSError, ValueError):
            return False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---------------------------------------------------------------- stream
    def payload(self, system: str, messages: list[Message], tools: tuple[ToolSpec, ...],
                reasoning: ReasoningMode = ReasoningMode.OFF) -> dict[str, Any]:
        """Build the `/api/chat` body.

        Two things here are load-bearing:

        `think` is sent unless we know the model rejects it. Omitting the field is NOT a
        safe default — for a reasoning model it means "think as much as you like", which
        measured ~17 s to first token. The safe default is an explicit `false`.

        `num_predict` grows when reasoning is on, because Ollama spends thinking tokens
        from the same allowance as the reply. Measured: `think: true` with
        `num_predict: 120` returned `done_reason: "length"`, 602 characters of thinking and
        an **empty** reply. Enabling reasoning without raising the allowance converts a slow
        answer into no answer at all.
        """
        thinking = reasoning.thinks
        body: dict[str, Any] = {
            "model": self.info.model,
            "messages": to_wire(system, messages),
            "stream": True,
            "keep_alive": self._keep_alive,
            "options": {
                "temperature": self._temperature,
                "num_ctx": self._num_ctx,
                "num_predict": self._num_predict_thinking if thinking else self._num_predict,
                # Stop the model narrating a fence it was told never to write.
                "stop": ["<<<ARS-EXTERNAL-"],
            },
        }
        think = reasoning.ollama_think
        if think is not None and self._thinking_supported is not False:
            body["think"] = think
        if tools:
            body["tools"] = to_ollama(tools)
        return body

    async def stream(
        self, *, system: str, messages: list[Message], tools: tuple[ToolSpec, ...],
        language: Language, reasoning: ReasoningMode = ReasoningMode.OFF,
    ) -> AsyncIterator[str | ToolCall]:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        stats = StreamStats(model=self.info.model)
        stats.extra["reasoning"] = reasoning.value
        self.last_stats = stats
        body = self.payload(system, messages, tools, reasoning)

        try:
            async with self._client.stream("POST", "/api/chat", json=body) as response:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", "replace")[:400]
                    if "think" in body and _rejected_think(response.status_code, detail):
                        # This model does not take the field. Remember it, drop it, and
                        # try once more rather than failing a turn over a hint. Costs one
                        # round trip, once per process; `capabilities()` at startup avoids
                        # even that.
                        self._thinking_supported = False
                        stats.extra["think_rejected"] = detail[:120]
                        body = self.payload(system, messages, tools, reasoning)
                        async for piece in self.stream(
                            system=system, messages=messages, tools=tools,
                            language=language, reasoning=ReasoningMode.PROVIDER_DEFAULT,
                        ):
                            yield piece
                        return
                    raise BackendUnavailable("ollama", f"HTTP {response.status_code}: {detail}")

                async for line in response.aiter_lines():
                    if self.cancelled:
                        stats.cancelled = True
                        # Leaving the `async with` closes the connection, which is what
                        # actually aborts generation on the server.
                        break
                    text, calls, done, metrics = parse_chunk(line)
                    if "thinking_chars" in metrics:
                        stats.extra["thinking_chars"] = (
                            stats.extra.get("thinking_chars", 0) + metrics["thinking_chars"]
                        )
                    if text:
                        if stats.first_token_ms is None:
                            stats.first_token_ms = (loop.time() - t0) * 1000.0
                        yield text
                    for call in calls:
                        if stats.first_token_ms is None:
                            stats.first_token_ms = (loop.time() - t0) * 1000.0
                        stats.tool_calls += 1
                        yield call
                    if done:
                        stats.input_tokens = int(metrics.get("prompt_eval_count", 0))
                        stats.output_tokens = int(metrics.get("eval_count", 0))
                        stats.extra |= metrics
                        if metrics.get("done_reason") == "length" and not stats.output_tokens:
                            stats.extra["empty_reply"] = True
                        break
        except httpx.ConnectError as exc:
            raise BackendUnavailable(
                "ollama", f"cannot reach {self._host} — is `ollama serve` running?"
            ) from exc
        except httpx.ReadTimeout as exc:
            raise BackendUnavailable("ollama", "read timeout while streaming") from exc
        except httpx.HTTPError as exc:
            raise BackendUnavailable("ollama", f"{type(exc).__name__}: {exc}") from exc
        finally:
            stats.total_ms = (loop.time() - t0) * 1000.0


def _rejected_think(status: int, detail: str) -> bool:
    """Did the server refuse specifically because of the `think` field?

    Verified against a live server: Ollama *ignores* unrecognised top-level fields (an
    unknown key returns 200), but validates `think` strictly — a bad value returns 400 with
    `invalid think value: ... (must be "high", "medium", "low", "max", true, or false)`.
    What a model without the `thinking` capability does is not documented, so this matches
    on the field name in any 4xx and treats that as "drop it and retry". A false positive
    costs one extra request; a false negative would fail the turn.
    """
    return 400 <= status < 500 and "think" in detail.lower()


__all__ = ["OllamaBackend", "parse_chunk", "to_wire"]
