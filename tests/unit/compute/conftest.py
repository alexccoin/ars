"""Pytest fixtures for the compute unit tests. Shared fakes live in `helpers.py` so the
eval harness in research/benchmarks can import them too."""

from __future__ import annotations

import pytest
from ars_protocol import Device, Session
from helpers import FakeGuard


@pytest.fixture
def session() -> Session:
    return Session(device=Device.DESKTOP)


@pytest.fixture
def guard() -> FakeGuard:
    return FakeGuard()
