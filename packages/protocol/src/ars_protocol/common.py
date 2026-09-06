"""Primitives shared by every A.R.S contract."""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


def new_id(prefix: str) -> str:
    """Prefixed, sortable-enough identifier. Prefix makes IDs self-describing in logs."""
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def now_ms() -> int:
    """Wall-clock milliseconds. All protocol timestamps use this unit — never seconds, never
    floats."""
    return int(time.time() * 1000)


class Model(BaseModel):
    """Base for every protocol message.

    Frozen because a message that mutates after it crosses a boundary is a bug that is
    almost impossible to trace. Extra fields are forbidden so a typo'd field name fails
    loudly at the edge instead of being silently dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class Language(StrEnum):
    """Languages A.R.S understands and speaks.

    Kept deliberately small: every language here must have a verified ASR model, a TTS
    voice, and reviewed system prompts. Adding one is a project, not a config change.
    """

    EN = "en"
    RO = "ro"

    @property
    def display_name(self) -> str:
        return {Language.EN: "English", Language.RO: "Română"}[self]


DEFAULT_LANGUAGE = Language.EN
SUPPORTED_LANGUAGES: tuple[Language, ...] = (Language.EN, Language.RO)

SessionId = Annotated[str, Field(pattern=r"^ses_[a-f0-9]{20}$")]
TurnId = Annotated[str, Field(pattern=r"^trn_[a-f0-9]{20}$")]
GrantId = Annotated[str, Field(pattern=r"^grn_[a-f0-9]{20}$")]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class Device(StrEnum):
    """Where a session physically lives. Guard policy differs per device class:
    a phone in a pocket is not the same trust environment as a desktop at home."""

    DESKTOP = "desktop"
    PHONE = "phone"
    WEB = "web"
    CLI = "cli"
    HEADLESS = "headless"
