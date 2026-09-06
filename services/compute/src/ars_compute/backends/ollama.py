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

Cancellation matters more here than anywhere else in the codebase. Barge-in has to *stop
generation*, not stop listening: a 14B model on a laptop that keeps decoding after the
user interrupts is holding the GPU that the next turn needs. Closing the streaming
response terminates the HTTP request, and Ollama aborts the running generation when its
client disconnects. That is why this file owns the transport rather than delegating to a
client library whose cancellation semantics we would have to trust.

Ollama is NOT installed on the development machine this was written on. This module is
written against the documented API and exercised by `tests/unit/test_ollama_wire.py`,
which drives `_parse_chunk` and the message conversion over recorded fixtures. The first
thing to do on a machine that has Ollama is run `research/benchmarks/compute/run.py
--backend ollama` and fill in the latency column with measured numbers instead of the
budget.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from ars_protocol import Language, ToolCall, ToolSpec

from ..context import Role
from ..errors import BackendUnavailable
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
    """One NDJSON line -> (text delta, tool calls, done, metrics).

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
    metrics = {
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
        self._keep_alive = keep_alive
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
                "the dominant term in first-token latency on an M-series laptop."
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
    def payload(self, system: str, messages: list[Message],
                tools: tuple[ToolSpec, ...]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.info.model,
            "messages": to_wire(system, messages),
            "stream": True,
            "keep_alive": self._keep_alive,
            "options": {
                "temperature": self._temperature,
                "num_ctx": self._num_ctx,
                "num_predict": self._num_predict,
                # Stop the model narrating a fence it was told never to write.
                "stop": ["<<<ARS-EXTERNAL-"],
            },
        }
        if tools:
            body["tools"] = to_ollama(tools)
        return body

    async def stream(
        self, *, system: str, messages: list[Message], tools: tuple[ToolSpec, ...],
        language: Language,
    ) -> AsyncIterator[str | ToolCall]:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        stats = StreamStats(model=self.info.model)
        self.last_stats = stats
        body = self.payload(system, messages, tools)

        try:
            async with self._client.stream("POST", "/api/chat", json=body) as response:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", "replace")[:400]
                    raise BackendUnavailable("ollama", f"HTTP {response.status_code}: {detail}")

                async for line in response.aiter_lines():
                    if self.cancelled:
                        stats.cancelled = True
                        # Leaving the `async with` closes the connection, which is what
                        # actually aborts generation on the server.
                        break
                    text, calls, done, metrics = parse_chunk(line)
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
                        stats.extra = metrics
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


__all__ = ["OllamaBackend", "parse_chunk", "to_wire"]
