"""Shared fixtures for the voice unit tests.

Everything here runs on mock engines: no weights, no microphone, no network. That is a
requirement, not a convenience — CI has none of those, and a test suite that needs them
stops being run.
"""

from __future__ import annotations

import pytest
from ars_protocol import FRAME_MS
from ars_voice.config import VoicePipelineConfig


@pytest.fixture
def config() -> VoicePipelineConfig:
    return VoicePipelineConfig()


@pytest.fixture
def frame_ms() -> int:
    return FRAME_MS
