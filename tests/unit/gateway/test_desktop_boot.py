"""The desktop shell's view of its own gateway.

This file exists because of one line. `start_gateway(host=...)` gained a None default so
it could fall back to ARS_LISTEN_HOST, and the URL the shell polls was still built from
that parameter — so it became "http://None:<port>", and the window spent ninety seconds
health-checking a hostname that does not exist while the gateway it had just started
answered every request in nine milliseconds. The user saw "starting the gateway…", then a
screen claiming A.R.S could not start, and a Retry button that did nothing.

What it binds to and what the shell talks to are different questions. These tests hold
them apart.
"""

from __future__ import annotations

import inspect

from ars_desktop import server


def test_the_shell_always_talks_to_loopback() -> None:
    """Whatever interface the gateway is exposed on, the window reaches it over loopback.
    That address is not configurable and must not become so."""
    source = inspect.getsource(server.start_gateway)
    assert 'base_url = f"http://127.0.0.1:{port}"' in source


def test_the_base_url_is_never_built_from_the_host_parameter() -> None:
    """The exact regression: `host` is Optional, and formatting an Optional into a URL
    produces a URL that resolves to nothing and fails slowly."""
    source = inspect.getsource(server.start_gateway)
    assert 'f"http://{host}:{port}"' not in source


def test_the_bind_address_still_honours_the_setting() -> None:
    """Fixing the URL must not quietly re-close the gateway to other devices."""
    source = inspect.getsource(server.start_gateway)
    assert 'os.environ.get("ARS_LISTEN_HOST", "127.0.0.1")' in source
    assert "host=bind" in source


def test_a_boot_already_running_is_reported_not_swallowed() -> None:
    """Retry pressed on a stuck window did nothing at all, silently, because the first
    boot still held the lock."""
    from ars_desktop.__main__ import DesktopShell

    source = inspect.getsource(DesktopShell.boot_or_diagnose)
    assert "boot already in progress" in source
