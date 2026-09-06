"""Shared backend machinery. No vendor SDK is imported in this file, or anywhere outside
the single module that implements that vendor's backend — CLAUDE.md rule 2.

Note on HTTP: neither backend uses a vendor SDK at all. `OllamaBackend` and
`AnthropicBackend` both speak their provider's documented HTTP API through `httpx`. That
is a deliberate choice, not laziness:

  * streaming and *cancellation* are the two behaviours the voice pipeline depends on,
    and both are properties of the transport. Owning the transport means barge-in closes
    the socket rather than hoping the SDK's context manager gets there;
  * the SDKs disagree about how to surface partial JSON for tool arguments, and this
    codebase needs one accumulation strategy it can test;
  * one less dependency that can pull in a different pydantic.

The provider-specific wire shapes are still confined to their own module, so the rule
holds and swapping in an SDK later touches one file.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from ars_core import LlmBackend
from ars_protocol import ContentBlock, Language, ToolCall, ToolSpec

from ..context import AssembledContext, ContextAssembler, Role, role_of
from ..tokens import FREE, Price


@dataclass(frozen=True)
class BackendInfo:
    """What a model choice actually costs. Every field here is a number someone can
    argue with, which is the point: model selection is a documented decision, not a
    preference."""

    name: str
    model: str
    runs_locally: bool
    price: Price = FREE
    context_window: int = 8192
    supports_tools: bool = True
    notes: str = ""


@dataclass(frozen=True)
class Message:
    """Provider-neutral message. Backends translate this into their own wire shape and
    nothing else."""

    role: Role
    text: str = ""
    tool_call: ToolCall | None = None
    tool_result_for: str | None = None
    """Set when this message carries the output of a tool call with that id."""


def merge_messages(messages: list[Message]) -> list[Message]:
    """Collapse consecutive same-role text messages.

    Providers vary in how tolerant they are of consecutive same-role turns, and the ones
    that tolerate it still bill for the extra structure. Merging is safe here because the
    quarantine frames survive the join — each framed block keeps its own fence, so two
    merged external blocks do not become one unfenced blob.
    """
    out: list[Message] = []
    for m in messages:
        if (
            out and out[-1].role is m.role and m.tool_call is None
            and m.tool_result_for is None and out[-1].tool_call is None
            and out[-1].tool_result_for is None
        ):
            out[-1] = Message(role=m.role, text=f"{out[-1].text}\n\n{m.text}")
        else:
            out.append(m)
    return out


class BaseBackend(LlmBackend):
    """Implements the `LlmBackend` seam and adds one extra entry point.

    `complete()` is the interface method and works with a bare `Sequence[ContentBlock]`,
    exactly as `packages/core` declares it. `complete_context()` is an additive
    convenience that takes the assembler's richer output so tool-call ids and slot
    information are not thrown away and re-guessed. Nothing outside services/compute needs
    the second one, and no protocol type was forked to add it.
    """

    info: BackendInfo

    def __init__(self) -> None:
        self._cancel = asyncio.Event()

    # ---------------------------------------------------------------- interface
    @property
    def runs_locally(self) -> bool:
        return self.info.runs_locally

    async def cancel(self) -> None:
        """Barge-in. Sets the flag every stream loop checks and every request is raced
        against; the concrete backends also tear down the socket so the provider stops
        generating rather than merely stops being listened to."""
        self._cancel.set()

    def reset(self) -> None:
        self._cancel = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    async def complete(
        self,
        *,
        system: str,
        context: Sequence[ContentBlock],
        tools: Sequence[ToolSpec] = (),
        language: Language,
    ) -> AsyncIterator[str | ToolCall]:
        self.reset()
        return self.stream(
            system=system,
            messages=self.messages_from_blocks(tuple(context), language),
            tools=tuple(tools),
            language=language,
        )

    async def complete_context(
        self, ctx: AssembledContext, *, tools: Sequence[ToolSpec] = ()
    ) -> AsyncIterator[str | ToolCall]:
        self.reset()
        return self.stream(
            system=ctx.system,
            messages=self.messages_from_context(ctx),
            tools=tuple(tools),
            language=ctx.reply_language,
        )

    @abstractmethod
    def stream(
        self, *, system: str, messages: list[Message], tools: tuple[ToolSpec, ...],
        language: Language,
    ) -> AsyncIterator[str | ToolCall]:
        """The one method a concrete backend implements. An async generator."""

    # ---------------------------------------------------------------- conversion
    @staticmethod
    def messages_from_context(ctx: AssembledContext) -> list[Message]:
        msgs: list[Message] = []
        for item in ctx.items:
            if item.tool_call is not None:
                msgs.append(Message(role=Role.ASSISTANT, tool_call=item.tool_call))
            elif item.tool_call_id is not None:
                msgs.append(Message(role=Role.USER, text=item.rendered,
                                    tool_result_for=item.tool_call_id))
            else:
                msgs.append(Message(role=item.role, text=item.rendered))
        return _ensure_user_first(merge_messages(msgs))

    @staticmethod
    def messages_from_blocks(blocks: tuple[ContentBlock, ...], language: Language) -> list[Message]:
        """Used by the plain `complete()` path. Blocks are framed here, so a caller that
        skips the assembler still cannot get untrusted text into a prompt unquarantined."""
        a = ContextAssembler()
        turn_id = "trn_" + "0" * 20
        msgs = [
            Message(role=role_of(b),
                    text=a.render(b, language, turn_id=turn_id, index=i)[0])
            for i, b in enumerate(blocks)
        ]
        return _ensure_user_first(merge_messages(msgs))


def _ensure_user_first(messages: list[Message]) -> list[Message]:
    """Providers require the conversation to open on the user's side. A context that
    starts with recalled memory (SYSTEM/USER_DATA framed) would otherwise 400."""
    msgs = [m for m in messages if m.role is not Role.SYSTEM or m.text.strip()]
    system_like = [m for m in msgs if m.role is Role.SYSTEM]
    rest = [m for m in msgs if m.role is not Role.SYSTEM]
    if system_like:
        # Fold stray SYSTEM-role context into the first user message rather than emitting
        # a second system block: two system prompts is an ambiguity the model resolves
        # unpredictably.
        folded = "\n\n".join(m.text for m in system_like)
        rest.insert(0, Message(role=Role.USER, text=folded))
    if rest and rest[0].role is not Role.USER:
        rest.insert(0, Message(role=Role.USER, text="(continuing)"))
    return merge_messages(rest)


@dataclass
class StreamStats:
    """Per-call numbers. Reported for every turn because the latency budget in
    docs/architecture/overview.md is a test, not an aspiration."""

    first_token_ms: float | None = None
    total_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    cancelled: bool = False
    model: str = ""
    extra: dict = field(default_factory=dict)

    def cost_usd(self, price: Price) -> float:
        return price.cost(self.input_tokens, self.output_tokens)


__all__ = ["BackendInfo", "BaseBackend", "Message", "StreamStats", "merge_messages"]
