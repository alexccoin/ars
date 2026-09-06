"""One helper, for one papercut.

`ars_core.interfaces.LlmBackend.complete` is declared `async def ... -> AsyncIterator[...]`.
That is a coroutine returning an iterator, so the correct call is:

    async for piece in await backend.complete(...):

The `for ... in await` reads like a typo and everybody writes it wrong the first time. The
signature lives in `packages/core`, which is contract owned by `system-architect`, so this
service does not change it unilaterally. Instead:

  * `BaseBackend.complete()` returns an object that is both awaitable and async-iterable,
    so both spellings work against any backend in this service;
  * `stream_reply()` below works against *any* `LlmBackend`, including third-party ones
    that follow the literal contract, and needs no `await` at the call site.

If the interface is ever simplified to a plain `def`, this module keeps working and can be
deleted in the same change.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Sequence

from ars_core import LlmBackend
from ars_protocol import ContentBlock, Language, ToolCall, ToolSpec

from .reasoning import ReasoningMode


async def stream_reply(
    backend: LlmBackend,
    *,
    system: str,
    context: Sequence[ContentBlock],
    tools: Sequence[ToolSpec] = (),
    language: Language,
    reasoning: ReasoningMode = ReasoningMode.OFF,
) -> AsyncIterator[str | ToolCall]:
    """Stream text deltas and tool calls from any backend. Just iterate it.

        async for piece in stream_reply(backend, system=..., context=..., language=...):
            ...

    `reasoning` is passed only to backends that accept it, so a bare `LlmBackend`
    implementation is not broken by an argument it never declared.
    """
    kwargs = {"system": system, "context": context, "tools": tools, "language": language}
    try:
        params = inspect.signature(type(backend).complete).parameters
    except (TypeError, ValueError):
        params = {}
    if "reasoning" in params:
        kwargs["reasoning"] = reasoning

    result = backend.complete(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    async for piece in result:
        yield piece


__all__ = ["stream_reply"]
