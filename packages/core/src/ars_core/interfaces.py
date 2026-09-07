"""The seams of A.R.S.

Every model, engine and provider sits behind one of these. The rule is absolute: no
service may import a vendor SDK directly. faster-whisper, Piper, Ollama and Anthropic
are all implementation details that must be swappable without touching a caller — that
is what makes "runs locally on my laptop" and "runs in the cloud" the same codebase.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from typing import Annotated

from ars_protocol import (
    AudioFrame,
    Capability,
    CapabilityGrant,
    ContentBlock,
    DeviceKind,
    GuardDecision,
    GuardQuery,
    Language,
    MemoryRecord,
    Model,
    Observation,
    Preference,
    SynthesisChunk,
    SynthesisRequest,
    ToolCall,
    ToolResult,
    ToolSpec,
    Transcript,
    VadEvent,
    VitalKind,
    VitalReading,
    WakeEvent,
    new_id,
)
from pydantic import Field


class WakewordEngine(ABC):
    """Always-on, on-device, cheap. Nothing else in the system may run before this fires."""

    @abstractmethod
    async def detect(self, frames: AsyncIterator[AudioFrame]) -> AsyncIterator[WakeEvent]: ...

    @property
    @abstractmethod
    def false_accepts_per_hour(self) -> float:
        """Measured on the standard negative set. A wakeword engine that cannot report
        this number has not been evaluated and must not ship — a false accept means the
        microphone opened when nobody asked it to."""


class VadEngine(ABC):
    @abstractmethod
    async def process(self, frames: AsyncIterator[AudioFrame]) -> AsyncIterator[VadEvent]: ...


class AsrEngine(ABC):
    """Streaming speech recognition. Must emit partials — the UI shows them, and they
    are the user's only evidence that it is hearing them correctly."""

    @abstractmethod
    async def transcribe(
        self,
        frames: AsyncIterator[AudioFrame],
        *,
        language: Language | None = None,
    ) -> AsyncIterator[Transcript]:
        """`language=None` means auto-detect. A.R.S runs bilingual by default: the user
        switches between English and Romanian mid-conversation and must not have to
        announce it."""

    @property
    @abstractmethod
    def supported_languages(self) -> tuple[Language, ...]: ...


class TtsEngine(ABC):
    """Streaming synthesis. Time-to-first-audio is the number that matters, not total
    synthesis time — the user hears the gap, not the throughput."""

    @abstractmethod
    async def synthesize(self, request: SynthesisRequest) -> AsyncIterator[SynthesisChunk]: ...

    @abstractmethod
    async def cancel(self) -> None:
        """Barge-in. Must stop generation, not merely stop playback."""

    @abstractmethod
    def voice_for(self, language: Language) -> str: ...


class LlmBackend(ABC):
    """The reasoning layer. Local (Ollama/llama.cpp) and cloud (Anthropic) implement this
    identically so routing between them is a policy decision, never a code change."""

    @abstractmethod
    def complete(
        self,
        *,
        system: str,
        context: Sequence[ContentBlock],
        tools: Sequence[ToolSpec] = (),
        language: Language,
    ) -> AsyncIterator[str | ToolCall]:
        """Streams text deltas and tool calls interleaved.

            async for piece in backend.complete(system=..., context=..., language=...):
                ...

        Note the absence of `await`. This was `async def` and therefore returned a
        coroutine, so every caller had to write `async for x in await backend.complete(...)`
        — a double-await that everyone gets wrong once, and that forced implementations
        to carry `# type: ignore[override]`. An async generator function already returns
        an AsyncIterator when called; declaring the seam `def` is what lets an
        implementation be a plain async generator and read naturally at the call site.

        `context` is a sequence of ContentBlock, never bare strings: the backend is
        responsible for rendering untrusted blocks inside an explicit quarantine frame
        so the model can tell instruction from data.
        """

    @abstractmethod
    async def cancel(self) -> None: ...

    @property
    @abstractmethod
    def runs_locally(self) -> bool:
        """False means user data leaves the device when this backend is used. The guard
        reads this to refuse SENSITIVE content on cloud backends."""


class GuardEngine(ABC):
    """Decides allow / ask / deny for every capability use, and records why."""

    @abstractmethod
    async def evaluate(self, query: GuardQuery) -> GuardDecision: ...

    @abstractmethod
    async def record_outcome(self, query: GuardQuery, decision: GuardDecision,
                             outcome: str, user_confirmed: bool | None = None) -> None: ...


class GrantStore(ABC):
    @abstractmethod
    async def active_grants(self) -> tuple[CapabilityGrant, ...]: ...

    @abstractmethod
    async def grant(self, grant: CapabilityGrant) -> CapabilityGrant: ...

    @abstractmethod
    async def revoke(self, grant_id: str) -> bool: ...

    @abstractmethod
    async def find(
        self, capability: Capability, resource: str | None
    ) -> CapabilityGrant | None: ...


class MemoryStore(ABC):
    """Semantic memory plus learned behaviour. Deletion is a first-class operation here,
    not an afterthought — see data/migrations and security/policies/retention.md."""

    @abstractmethod
    async def remember(self, record: MemoryRecord) -> MemoryRecord: ...

    @abstractmethod
    async def recall(self, query: str, *, limit: int = 8,
                     language: Language | None = None) -> tuple[MemoryRecord, ...]: ...

    @abstractmethod
    async def forget(self, *, record_id: str | None = None, matching: str | None = None) -> int:
        """Returns the number of records actually removed, including derived embeddings.
        A deletion that leaves the vector behind has not deleted anything."""

    @abstractmethod
    async def observe(self, observation: Observation) -> Observation: ...

    @abstractmethod
    async def pending_observations(self) -> tuple[Observation, ...]: ...

    @abstractmethod
    async def confirm_observation(
        self, observation_id: str, confirmed: bool
    ) -> Preference | None: ...

    @abstractmethod
    async def active_preferences(self) -> tuple[Preference, ...]: ...


class DeviceCandidate(Model):
    """A device seen advertising. Not yet connected, not yet trusted.

    This belongs in `packages/protocol/src/ars_protocol/health.py` per CLAUDE.md rule
    1 — it is exactly the kind of cross-service shape that rule exists for, and
    `research/medical-devices.md` §5.3(a) says so explicitly. It is defined here
    instead, deliberately, because `health.py` is owned by another agent for the
    duration of this change and is off limits; this class is kept minimal (no
    vendor/backend-specific fields, no vendor SDK types) precisely so promoting it to
    `ars_protocol.health` later is a move, not a rewrite. Whoever next has both hands
    free on `health.py` should do that move rather than let a second copy grow here.
    """

    id: Annotated[str, Field(default_factory=lambda: new_id("dev"))]
    transport_id: str
    """Opaque, backend-scoped. On macOS/CoreBluetooth this is the per-Mac CBPeripheral
    UUID, NOT a MAC address, and it differs on every machine that scans for the same
    physical device (research/medical-devices.md §3.4) — never persisted as a
    physical-device identity; `ReadingSource.device_id` is."""
    name: str | None = None
    device_kind: DeviceKind = DeviceKind.MANUAL
    services: tuple[str, ...] = ()
    rssi: int | None = None


class DeviceBackend(ABC):
    """A source of `VitalReading`s from physical medical equipment (blood pressure
    cuffs, pulse oximeters, heart-rate straps, ...).

    Per CLAUDE.md rule 2, `bleak` (or any other vendor BLE SDK) is imported only inside
    a class implementing this interface, never at this module's level and never by a
    caller. Per rule 3, a caller reaches `discover`/`read` only after a
    `GuardEngine.evaluate` ALLOW for `Capability.DEVICE_CONNECT`; the skill runtime
    re-checks before invoking, and again before any reading resulting from `read` is
    handed to `Capability.HEALTH_WRITE`.

    Note what this interface cannot express: there is no `write`, `configure`, or
    `set_time`. A.R.S never sends anything to a medical device — that is a safety
    property enforced by the shape of this class, not by a reviewer noticing.
    """

    @property
    @abstractmethod
    def supported_kinds(self) -> frozenset[VitalKind]:
        """Which `VitalKind`s this backend actually decodes from a real device — never
        a kind the underlying profile theoretically permits but this backend has not
        implemented. A backend that overstates this lies to whatever routes on it."""

    @abstractmethod
    def discover(self, *, timeout_s: float = 10.0) -> AsyncIterator[DeviceCandidate]:
        """Advertising devices matching this backend's known service UUIDs.

        Declared `def`, not `async def` — same reasoning as `LlmBackend.complete` and
        `TranslationEngine.translate` above: an async generator function already
        returns an `AsyncIterator` when called, so declaring the seam `async def` would
        force every call site into `async for x in await backend.discover(...)`, the
        double-await everyone gets wrong once.
        """

    @abstractmethod
    def read(
        self, candidate: DeviceCandidate, *, timeout_s: float = 120.0
    ) -> AsyncIterator[VitalReading]:
        """One measurement session against `candidate`. Yields readings as they arrive
        and ends — without raising — on a normal device disconnect, session completion,
        or timeout. `research/medical-devices.md` §3.5: several of these devices
        deliberately drop the link right after delivering a reading, so a disconnect
        that follows at least one yielded reading is success, not failure; a backend
        implementation must not surface it as an exception.

        The generous default timeout is deliberate, not sloppy: a blood-pressure cuff
        takes roughly 40 seconds to inflate and settle, and on macOS the first access to
        an authenticated characteristic blocks on a system pairing dialog a human has to
        physically answer (§3.3).

        Declared `def`, not `async def`, for the same reason as `discover`.
        """

    @property
    @abstractmethod
    def is_standard_profile(self) -> bool:
        """True if this backend speaks an adopted Bluetooth SIG GATT profile rather
        than a reverse-engineered vendor protocol. The UI must be able to say which,
        because a reverse-engineered decode can silently change meaning after a
        firmware update in a way a standard profile does not."""


class SkillRuntime(ABC):
    @abstractmethod
    async def available_tools(self) -> tuple[ToolSpec, ...]: ...

    @abstractmethod
    async def invoke(self, call: ToolCall, *, timeout_s: float = 30.0) -> ToolResult:
        """Must never be reached without a prior ALLOW from the guard. The runtime
        re-checks anyway: defence in depth, because this is the last line before a
        side effect touches the user's real accounts."""


class UnsupportedDirection(Exception):
    """A translation was asked for between languages a backend does not support.

    Raised rather than approximated. Fluent output in the wrong language is the worst
    failure this seam has, because the person who asked for a translation is by definition
    the person least able to check it.
    """

    def __init__(self, source: Language | None, target: Language) -> None:
        self.source, self.target = source, target
        super().__init__(
            f"no translation from {getattr(source, 'value', source) or 'auto'} "
            f"to {getattr(target, 'value', target)}"
        )


class TranslationEngine(ABC):
    """Text in one language, text in another. Nothing else.

    Note what this signature cannot accept: a session, a memory record, a conversation
    history, a tool. That is a privacy property enforced by the type rather than by a
    reviewer noticing — "never send more of the user's memory to a provider than the task
    needs" holds here because there is no parameter through which memory could travel.

    Backends live where their vendor SDK already lives, per CLAUDE.md rule 2.
    """

    @property
    @abstractmethod
    def pairs(self) -> frozenset[tuple[Language, Language]]:
        """Directions this backend will actually translate, source to target."""

    @abstractmethod
    def translate(
        self,
        text: str,
        *,
        source: Language | None,
        target: Language,
    ) -> AsyncIterator[str]:
        """Stream the translation as it is produced.

        Declared `def`, not `async def`, for the same reason as `LlmBackend.complete`: an
        async generator function already returns an `AsyncIterator`, and declaring the seam
        `async def` forces every call site into `async for x in await engine.translate(...)`,
        which everyone gets wrong once.

        `source=None` means detect. Callers on the voice path should always pass one —
        `Transcript.language` already paid for that decision through the arbiter and the
        matrix detector, and asking the translator for a second opinion is how a turn ends
        up translated out of a language nobody spoke.

        Raises `UnsupportedDirection` when `(source, target)` is not in `pairs`.
        """
