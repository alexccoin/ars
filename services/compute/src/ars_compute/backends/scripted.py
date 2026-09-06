"""A deterministic backend.

Every eval and every unit test in this service runs against this. Ollama is not installed
on this machine and the Anthropic API costs money and varies run to run; neither is a
basis for an assertion. What the tests here are actually asserting is *our* behaviour —
did the quarantine frame reach the model, was the reply language resolved correctly, did a
tainted turn reach the guard with `tainted=True`, did cancellation stop the stream — and
all of that is observable with a scripted model and none of it needs a real one.

It also records every invocation, which is how a test proves a negative: that the string
"ignore your instructions" arrived inside a fence, and that the system prompt telling the
model to refuse it was present.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from ars_protocol import Language, ToolCall, ToolSpec

from ..tokens import DEFAULT_ESTIMATOR, FREE
from .base import BackendInfo, BaseBackend, Message, StreamStats


@dataclass(frozen=True)
class Invocation:
    """Exactly what the backend was handed. Tests assert against this."""

    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    language: Language
    index: int

    @property
    def prompt(self) -> str:
        """Flat view of everything the model saw, system prompt included."""
        return self.system + "\n\n" + "\n\n".join(m.text for m in self.messages)

    def contains(self, needle: str) -> bool:
        return needle.lower() in self.prompt.lower()

    def search(self, pattern: str) -> bool:
        return re.search(pattern, self.prompt, re.IGNORECASE | re.DOTALL) is not None


@dataclass(frozen=True)
class Scene:
    """One scripted model response.

    `text` may be a per-language dict, which is what makes the language evals meaningful:
    the scene answers in whatever language the orchestrator asked for, so a wrong reply
    language is our bug and not the script's.
    """

    name: str
    text: str | dict[Language, str] = ""
    tool: tuple[str, dict] | None = None
    tool_first: bool = False
    when: Callable[[Invocation], bool] | None = None
    at_call: int | None = None
    """Match only on the Nth call within a turn (0-based). Lets one scene answer the
    first pass and another answer after the tool result comes back."""
    first_token_ms: float = 0.0
    chunk_ms: float = 0.0
    chunk_chars: int = 14
    hang_ms: float = 0.0
    """Sleep before producing anything. Used to test barge-in and the filler deadline."""
    raises: Exception | None = None

    def matches(self, inv: Invocation) -> bool:
        if self.at_call is not None and self.at_call != inv.index:
            return False
        return self.when(inv) if self.when else True

    def resolve_text(self, language: Language) -> str:
        if isinstance(self.text, dict):
            return self.text.get(language, self.text.get(Language.EN, ""))
        return self.text


class ScriptedBackend(BaseBackend):
    """Deterministic `LlmBackend`. `runs_locally=True` because it is in-process — a test
    that routes to it must not be able to accidentally assert cloud behaviour."""

    def __init__(
        self,
        scenes: list[Scene] | tuple[Scene, ...] = (),
        *,
        name: str = "scripted",
        model: str = "scripted-v1",
        runs_locally: bool = True,
        fallback: str | dict[Language, str] = "",
    ) -> None:
        super().__init__()
        self.scenes: list[Scene] = list(scenes)
        self.fallback = fallback
        self.invocations: list[Invocation] = []
        self.stats: list[StreamStats] = []
        self._call_index = 0
        self.info = BackendInfo(
            name=name, model=model, runs_locally=runs_locally, price=FREE,
            context_window=8192, supports_tools=True,
            notes="deterministic; for tests and evals only",
        )

    def new_turn(self) -> None:
        """Reset the per-turn call counter so `Scene.at_call` means what it says."""
        self._call_index = 0

    @property
    def last(self) -> Invocation:
        return self.invocations[-1]

    async def stream(
        self, *, system: str, messages: list[Message], tools: tuple[ToolSpec, ...],
        language: Language,
    ) -> AsyncIterator[str | ToolCall]:
        inv = Invocation(system=system, messages=tuple(messages), tools=tools,
                         language=language, index=self._call_index)
        self._call_index += 1
        self.invocations.append(inv)

        scene = next((s for s in self.scenes if s.matches(inv)), None)
        stats = StreamStats(model=self.info.model)
        stats.input_tokens = DEFAULT_ESTIMATOR.count(inv.prompt, language)
        self.stats.append(stats)

        if scene is None:
            fb = self.fallback
            text = fb.get(language, "") if isinstance(fb, dict) else fb
            scene = Scene(name="fallback", text=text)

        if scene.hang_ms:
            await self._sleep(scene.hang_ms / 1000)
        if scene.raises is not None:
            raise scene.raises

        async def emit_tool() -> AsyncIterator[ToolCall]:
            if scene.tool is not None:
                stats.tool_calls += 1
                yield ToolCall(tool=scene.tool[0], arguments=dict(scene.tool[1]))

        async def emit_text() -> AsyncIterator[str]:
            body = scene.resolve_text(language)
            if not body:
                return
            if scene.first_token_ms:
                await self._sleep(scene.first_token_ms / 1000)
            for i in range(0, len(body), scene.chunk_chars):
                if self.cancelled:
                    stats.cancelled = True
                    return
                chunk = body[i : i + scene.chunk_chars]
                stats.output_tokens += DEFAULT_ESTIMATOR.count(chunk, language)
                yield chunk
                if scene.chunk_ms:
                    await self._sleep(scene.chunk_ms / 1000)

        first, second = (emit_tool, emit_text) if scene.tool_first else (emit_text, emit_tool)
        async for piece in first():
            yield piece
        if self.cancelled:
            stats.cancelled = True
            return
        async for piece in second():
            yield piece

    async def _sleep(self, seconds: float) -> None:
        """Sleep that a cancel can cut short — otherwise a barge-in test would have to
        wait out the scripted latency it was meant to interrupt."""
        try:
            await asyncio.wait_for(self._cancel.wait(), timeout=seconds)
        except TimeoutError:
            pass


def scene_for_language(name: str, en: str, ro: str, **kw: object) -> Scene:
    return Scene(name=name, text={Language.EN: en, Language.RO: ro}, **kw)  # type: ignore[arg-type]


__all__ = ["Invocation", "Scene", "ScriptedBackend", "scene_for_language"]
