"""The wire protocol between an A.R.S client (desktop, phone, web, CLI) and the gateway.

Everything is a stream. There is no request/response mode: a voice assistant that waits
for a complete answer before speaking has already lost the latency budget.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from .audio import SynthesisChunk, Transcript, VadEvent, WakeEvent
from .capability import CapabilityGrant, GrantRequest
from .common import Device, Language, Model, SessionId, TurnId, new_id, now_ms
from .guard import GuardDecision
from .memory import Observation
from .tools import ToolCall, ToolResult


class AgentState(StrEnum):
    """What A.R.S is doing right now. The UI renders this honestly — a spinner that
    says "thinking" while the microphone is live is a privacy lie."""

    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    ACTING = "acting"
    SPEAKING = "speaking"
    WAITING_FOR_CONSENT = "waiting_for_consent"
    ERROR = "error"


class Session(Model):
    id: SessionId = Field(default_factory=lambda: new_id("ses"))
    device: Device
    started_at_ms: int = Field(default_factory=now_ms)
    preferred_language: Language | None = None
    """None means follow the language of each utterance, which is the default and the
    behaviour a bilingual household actually wants."""


class Turn(Model):
    id: TurnId = Field(default_factory=lambda: new_id("trn"))
    session_id: SessionId
    started_at_ms: int = Field(default_factory=now_ms)
    tainted: bool = False


# --------------------------------------------------------------------------- client -> server

class StartSession(Model):
    type: Literal["start_session"] = "start_session"
    device: Device
    preferred_language: Language | None = None
    client_version: str


class AudioChunk(Model):
    type: Literal["audio"] = "audio"
    seq: int
    pcm_b64: str
    """base64 of int16 PCM at the rate declared in audio.py. Binary frames are preferred
    on transports that support them; this field exists for JSON-only clients."""


class TextInput(Model):
    type: Literal["text"] = "text"
    text: str
    language: Language | None = None


class Interrupt(Model):
    """Barge-in. Must cancel synthesis, the in-flight model call, and any running tool
    that is cancellable — muting the speaker is not an interrupt."""

    type: Literal["interrupt"] = "interrupt"
    reason: Literal["barge_in", "user_cancel"] = "barge_in"


class ConsentResponse(Model):
    type: Literal["consent_response"] = "consent_response"
    request_id: str
    approved: bool
    remember: bool = False
    """If true the auth service converts this one-off approval into a standing grant."""


class ObservationResponse(Model):
    """The user answering "should I remember that you prefer X?"."""

    type: Literal["observation_response"] = "observation_response"
    observation_id: str
    confirmed: bool


ClientEvent = Annotated[
    StartSession | AudioChunk | TextInput | Interrupt | ConsentResponse | ObservationResponse,
    Field(discriminator="type"),
]
ClientEventAdapter: TypeAdapter[ClientEvent] = TypeAdapter(ClientEvent)


# --------------------------------------------------------------------------- server -> client

class SessionStarted(Model):
    type: Literal["session_started"] = "session_started"
    session: Session
    languages: tuple[Language, ...]
    server_version: str


class StateChanged(Model):
    type: Literal["state"] = "state"
    state: AgentState
    turn_id: TurnId | None = None


class WakeDetected(Model):
    type: Literal["wake"] = "wake"
    event: WakeEvent


class VoiceActivity(Model):
    type: Literal["vad"] = "vad"
    event: VadEvent


class TranscriptEvent(Model):
    type: Literal["transcript"] = "transcript"
    turn_id: TurnId
    transcript: Transcript


class ReplyDelta(Model):
    """Streamed text of the assistant's reply. TTS begins at the first sentence boundary,
    not at the end of the reply."""

    type: Literal["reply_delta"] = "reply_delta"
    turn_id: TurnId
    text: str
    language: Language


class ReplyDone(Model):
    type: Literal["reply_done"] = "reply_done"
    turn_id: TurnId
    text: str
    language: Language


class AudioOut(Model):
    type: Literal["audio_out"] = "audio_out"
    turn_id: TurnId
    chunk: SynthesisChunk


class ToolCallEvent(Model):
    """Surfaced to the UI so the user can see what it is doing while it does it.
    An assistant acting on private data behind an opaque spinner is not trustworthy."""

    type: Literal["tool_call"] = "tool_call"
    turn_id: TurnId
    call: ToolCall
    decision: GuardDecision


class ToolResultEvent(Model):
    type: Literal["tool_result"] = "tool_result"
    turn_id: TurnId
    result: ToolResult


class ConsentRequired(Model):
    type: Literal["consent_required"] = "consent_required"
    request_id: str = Field(default_factory=lambda: new_id("cns"))
    turn_id: TurnId
    request: GrantRequest
    spoken_prompt: str
    """Guard-composed, in the user's language. Never model-generated when the turn is
    tainted — otherwise the attacker writes the consent prompt."""


class GrantChanged(Model):
    type: Literal["grant_changed"] = "grant_changed"
    grant: CapabilityGrant


class ObservationProposed(Model):
    type: Literal["observation_proposed"] = "observation_proposed"
    observation: Observation
    spoken_prompt: str


class ErrorEvent(Model):
    type: Literal["error"] = "error"
    code: str
    message: str
    turn_id: TurnId | None = None
    recoverable: bool = True


ServerEvent = Annotated[
    SessionStarted | StateChanged | WakeDetected | VoiceActivity | TranscriptEvent | ReplyDelta |
    ReplyDone | AudioOut | ToolCallEvent | ToolResultEvent | ConsentRequired | GrantChanged |
    ObservationProposed | ErrorEvent,
    Field(discriminator="type"),
]
ServerEventAdapter: TypeAdapter[ServerEvent] = TypeAdapter(ServerEvent)
