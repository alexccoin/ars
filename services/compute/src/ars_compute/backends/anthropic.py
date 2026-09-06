"""Cloud reasoning through the Anthropic Messages API. `runs_locally = False`.

Opt-in per capability, never the default, and the `Router` will refuse to reach this file
at all if anything `SENSITIVE` is in the context.

Wire shape verified against platform.claude.com on 2026-09-06 (the `claude-api` skill this
task referred to is not installed in this checkout — `.claude/skills/` is empty — so the
published docs were used as the source of truth instead, and the specific pages are cited
below).

  * Model IDs — docs/en/about-claude/models/overview. `claude-sonnet-5` is a current,
    pinned, dateless snapshot: 1M context, 128k max output, $2/$10 per MTok. It is the
    default here because it is the cheapest current model whose multilingual quality we
    would accept for Romanian; `claude-haiku-4-5` is half the price but its reliable
    knowledge cutoff is Feb 2025 and it does not accept `effort`.
  * Streaming — docs/en/build-with-claude/streaming. SSE. Events: `message_start`,
    `content_block_start`, `content_block_delta` (`text_delta`, `input_json_delta`,
    `thinking_delta`, `signature_delta`), `content_block_stop`, `message_delta`,
    `message_stop`, `ping`, `error`. Unknown event types must be ignored, not fatal —
    the versioning policy says new ones get added.
  * Tool use — docs/en/agents-and-tools/tool-use/overview. Request: `tools: [{name,
    description, input_schema}]`. Response: a `tool_use` content block whose `input`
    arrives as *partial JSON strings* in `input_json_delta`, accumulated and parsed at
    `content_block_stop`. Results go back as a `tool_result` block in a user message,
    keyed by `tool_use_id`.
  * Effort — docs/en/build-with-claude/effort. `output_config.effort`. Defaults to `high`
    on Sonnet 5, which is wrong for us: the doc's own guidance is that `low` is "for
    high-volume or latency-sensitive workloads... where faster turnaround is prioritized",
    and this is a voice pipeline with a 200 ms first-token budget. So `low` is the default
    here and raising it is a deliberate, measured choice.

Two A.R.S-specific behaviours in this file:

  * `thinking_delta` is consumed and **never** emitted as reply text. Thinking is not the
    answer; streaming it into `ReplyDelta` would put it through TTS.
  * Tool-call ids. The protocol's `ToolCall.id` (`tc_…`) is what A.R.S audits, so that is
    what we keep. The provider's `toolu_…` id is remembered alongside it and used when the
    assistant turn is replayed, so the `tool_result.tool_use_id` still matches exactly.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
from ars_protocol import Language, ToolCall, ToolSpec

from ..context import Role
from ..errors import BackendUnavailable
from ..tokens import PRICES, Price
from ..toolschema import to_anthropic
from .base import BackendInfo, BaseBackend, Message, StreamStats

API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_BASE_URL = "https://api.anthropic.com"


class AnthropicBackend(BaseBackend):
    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        max_tokens: int = 1024,
        temperature: float | None = None,
        effort: str | None = "low",
        connect_timeout_s: float = 3.0,
        read_timeout_s: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__()
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._effort = effort
        self._provider_ids: dict[str, str] = {}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(read_timeout_s, connect=connect_timeout_s),
        )
        self.last_stats: StreamStats | None = None
        self.info = BackendInfo(
            name="anthropic", model=model, runs_locally=False,
            price=PRICES.get(model, Price(2.0, 10.0)),
            context_window=1_000_000, supports_tools=True,
            notes=(
                "opt-in per capability. User data leaves the device on every call, so the "
                "router refuses this backend outright when SENSITIVE content is present, "
                "and the cloud context budget carries a smaller memory allowance than the "
                "local one."
            ),
        )

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---------------------------------------------------------------- request
    def to_wire(self, messages: list[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        for m in messages:
            if m.tool_call is not None:
                wire.append({
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": self._provider_ids.get(m.tool_call.id, m.tool_call.id),
                        "name": m.tool_call.tool,
                        "input": m.tool_call.arguments,
                    }],
                })
            elif m.tool_result_for is not None:
                wire.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": self._provider_ids.get(m.tool_result_for, m.tool_result_for),
                        # The quarantine frame travels inside the tool_result content.
                        # A tool_result block is not a privileged channel and must not be
                        # treated as one just because the API gives it its own type.
                        "content": m.text,
                    }],
                })
            else:
                wire.append({
                    "role": "assistant" if m.role is Role.ASSISTANT else "user",
                    "content": [{"type": "text", "text": m.text}],
                })
        return wire

    def payload(self, system: str, messages: list[Message],
                tools: tuple[ToolSpec, ...]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.info.model,
            "max_tokens": self._max_tokens,
            "stream": True,
            "system": system,
            "messages": self.to_wire(messages),
        }
        if self._temperature is not None:
            body["temperature"] = self._temperature
        if self._effort:
            body["output_config"] = {"effort": self._effort}
        if tools:
            body["tools"] = to_anthropic(tools)
            # One tool at a time. A parallel batch cannot be shown to the user as a single
            # consent prompt, and the guard evaluates one capability per query.
            body["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        return body

    # ---------------------------------------------------------------- stream
    async def stream(
        self, *, system: str, messages: list[Message], tools: tuple[ToolSpec, ...],
        language: Language,
    ) -> AsyncIterator[str | ToolCall]:
        if not self._api_key:
            raise BackendUnavailable("anthropic", "ANTHROPIC_API_KEY is not set")

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        stats = StreamStats(model=self.info.model)
        self.last_stats = stats
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        blocks: dict[int, dict[str, Any]] = {}

        try:
            async with self._client.stream(
                "POST", "/v1/messages", json=self.payload(system, messages, tools),
                headers=headers,
            ) as response:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", "replace")[:400]
                    raise BackendUnavailable(
                        "anthropic", f"HTTP {response.status_code}: {detail}"
                    )

                event_name = ""
                async for line in response.aiter_lines():
                    if self.cancelled:
                        stats.cancelled = True
                        break  # closing the response aborts the request
                    if not line:
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    kind = data.get("type", event_name)

                    if kind == "error":
                        err = data.get("error", {})
                        raise BackendUnavailable(
                            "anthropic", f"{err.get('type', 'error')}: {err.get('message', '')}"
                        )

                    if kind == "message_start":
                        usage = (data.get("message") or {}).get("usage") or {}
                        stats.input_tokens = int(usage.get("input_tokens", 0))

                    elif kind == "content_block_start":
                        cb = data.get("content_block") or {}
                        blocks[data.get("index", 0)] = {
                            "type": cb.get("type"), "id": cb.get("id"),
                            "name": cb.get("name"), "json": "",
                        }

                    elif kind == "content_block_delta":
                        delta = data.get("delta") or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                if stats.first_token_ms is None:
                                    stats.first_token_ms = (loop.time() - t0) * 1000.0
                                yield text
                        elif dtype == "input_json_delta":
                            slot = blocks.setdefault(
                                data.get("index", 0), {"type": "tool_use", "json": ""}
                            )
                            slot["json"] = slot.get("json", "") + delta.get("partial_json", "")
                        # thinking_delta / signature_delta: consumed, never spoken.

                    elif kind == "content_block_stop":
                        slot = blocks.pop(data.get("index", 0), None)
                        if slot and slot.get("type") == "tool_use":
                            call = _tool_call_from(slot)
                            self._provider_ids[call.id] = slot.get("id") or call.id
                            if stats.first_token_ms is None:
                                stats.first_token_ms = (loop.time() - t0) * 1000.0
                            stats.tool_calls += 1
                            yield call

                    elif kind == "message_delta":
                        usage = data.get("usage") or {}
                        stats.output_tokens = int(usage.get("output_tokens", stats.output_tokens))
                        stats.extra["stop_reason"] = (data.get("delta") or {}).get("stop_reason")

                    elif kind == "message_stop":
                        break
                    # ping and any future event type: ignored on purpose.
        except httpx.ConnectError as exc:
            raise BackendUnavailable("anthropic", "cannot reach the API (offline?)") from exc
        except httpx.ReadTimeout as exc:
            raise BackendUnavailable("anthropic", "read timeout while streaming") from exc
        except httpx.HTTPError as exc:
            raise BackendUnavailable("anthropic", f"{type(exc).__name__}: {exc}") from exc
        finally:
            stats.total_ms = (loop.time() - t0) * 1000.0

    def cost_of_last_turn(self) -> float:
        return self.last_stats.cost_usd(self.info.price) if self.last_stats else 0.0


def _tool_call_from(slot: dict[str, Any]) -> ToolCall:
    raw = slot.get("json") or "{}"
    try:
        args = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        # A truncated stream leaves partial JSON. Surfacing it as an argument the guard
        # can see beats silently inventing an empty call.
        args = {"_partial_json": raw}
    return ToolCall(tool=slot.get("name") or "", arguments=args if isinstance(args, dict) else {})


__all__ = ["API_VERSION", "DEFAULT_MODEL", "AnthropicBackend"]
