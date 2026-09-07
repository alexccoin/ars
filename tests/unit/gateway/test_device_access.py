"""Who may talk to the gateway once it stops listening only on loopback.

A.R.S holds the user's documents, their learned preferences, and standing grants to act
on their real accounts. While it listened on loopback only, "can reach the socket" meant
"is already logged into this Mac" and no further check was needed. Opening it to a phone
changes that, and these are the tests that the change did not open it to everyone.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ars_gateway.access import (
    COOKIE, QUERY_PARAM, is_loopback, load_or_create_token,
)


def test_the_token_survives_a_restart(tmp_path: Path) -> None:
    """A token that changed on every start would re-pair every phone every morning, and a
    pairing flow performed daily is a pairing flow that gets disabled."""
    first = load_or_create_token(tmp_path)
    assert load_or_create_token(tmp_path) == first


def test_the_token_is_not_readable_by_other_users(tmp_path: Path) -> None:
    load_or_create_token(tmp_path)
    mode = (tmp_path / "device_token").stat().st_mode & 0o777
    assert mode == 0o600, f"device token is {oct(mode)}"


def test_the_token_is_long_enough_to_not_be_guessed(tmp_path: Path) -> None:
    assert len(load_or_create_token(tmp_path)) >= 32


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("::1", True), ("127.0.0.5", True),
    ("192.168.1.20", False), ("10.0.0.3", False), ("8.8.8.8", False),
    ("", False), (None, False), ("not-an-address", False),
])
def test_loopback_is_recognised_and_nothing_else_is(host, expected) -> None:
    """The whole access model rests on this one predicate. A hostname that fails to parse
    is not loopback — the safe direction, since the alternative is treating an unknown
    peer as the owner."""
    assert is_loopback(host) is expected


def test_the_names_the_client_and_server_must_agree_on() -> None:
    """The browser reads the token from ?t= and the server reads the cookie it set. If
    these drift apart, pairing silently stops working on the second page load."""
    assert QUERY_PARAM == "t"
    assert COOKIE == "ars_device"
