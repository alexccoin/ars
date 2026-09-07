"""Runs the real gateway (``ars_gateway.app:app``) in-process, on a free loopback port.

In-process, not a child process: a supervised subprocess buys nothing here (there is
nothing to isolate it from — one user, one machine) and costs the extra plumbing of
piping logs and forwarding signals across a process boundary. What actually matters —
graceful shutdown that drains the open WebSocket turn instead of killing it, and no
orphan left behind — is what this module gives up if it gets sloppy, so both are
explicit below: uvicorn's own graceful-shutdown path (bounded by
``timeout_graceful_shutdown``) runs the gateway's ``lifespan`` shutdown, and the whole
thing lives on a daemon thread so it can never outlive this process either way.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ars_protocol import SUPPORTED_LANGUAGES

from . import paths

log = logging.getLogger("ars.desktop.server")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _load_env_overrides() -> None:
    """A user-editable override file at ``~/Library/Application Support/A.R.S/.env``.

    Simple ``KEY=VALUE`` lines, applied on top of the desktop defaults below but before
    anything the gateway itself reads — so e.g. pointing at a different Ollama model
    doesn't need a rebuild. Never created automatically; never shipped.
    """
    override = paths.user_env_override_file()
    if not override.exists():
        return
    for line in override.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def configure_environment() -> None:
    """Set the environment the gateway reads its config from.

    Must run before ``ars_gateway.app`` (or anything it imports) is imported: the
    gateway's ``ArsConfig`` — and the module-level ``Ars()`` instance in
    ``ars_gateway.app`` — read the environment at *construction* time, not lazily.

    A double-clicked .app has no meaningful current working directory and no repo
    ``.env`` beside it (and must not — secrets do not enter the bundle). So instead of
    relying on ``ArsConfig``'s built-in ``.env`` file lookup, every value it needs a
    real answer for is set explicitly, pointed at the per-user Application Support
    directory rather than ``./var``.
    """
    support = paths.app_support_dir()
    data_dir = support / "var"
    data_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("ARS_ENV", "desktop")
    os.environ.setdefault("ARS_DATA_DIR", str(data_dir))
    os.environ.setdefault("ARS_GUARD_AUDIT_LOG", str(data_dir / "audit.jsonl"))
    # From the protocol, never a copy of it. This was the fourth hard-coded "en,ro" in
    # the tree, and the one that survived the other three being fixed — so the desktop
    # app, which is how A.R.S is actually used, was the only place still shipping without
    # German. It showed up as "Languages: en, ro" in the status panel of a running build.
    os.environ.setdefault("ARS_LANGUAGES", ",".join(l.value for l in SUPPORTED_LANGUAGES))
    os.environ.setdefault("ARS_MODELS_DIR", str(paths.resolve_models_dir()))

    _load_env_overrides()


@dataclass
class GatewayHandle:
    port: int
    base_url: str
    thread: threading.Thread
    _server: object  # uvicorn.Server, kept as object to avoid importing uvicorn at type-check time
    _loop: asyncio.AbstractEventLoop
    import_error: str | None = None

    def stop(self, timeout: float = 8.0) -> None:
        if self.import_error is not None:
            return
        log.info("stopping gateway (graceful, draining connections)")
        self._server.should_exit = True  # type: ignore[attr-defined]
        # uvicorn's serve loop polls should_exit; nudge the loop so it notices promptly
        # instead of waiting for its next poll tick.
        try:
            self._loop.call_soon_threadsafe(lambda: None)
        except RuntimeError:
            pass
        self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            log.warning("gateway thread did not stop within %.1fs", timeout)


def start_gateway(*, host: str | None = None) -> GatewayHandle:
    """Start the gateway on a free port and return immediately; call ``wait_healthy``
    to block until it can actually serve a request."""
    configure_environment()
    port = _free_port()
    # What it BINDS to and what the shell TALKS TO are different questions, and conflating
    # them cost an evening: when `host` gained a None default so it could fall back to
    # ARS_LISTEN_HOST, this line quietly produced "http://None:<port>", and the shell spent
    # ninety seconds health-checking a hostname that does not exist while the gateway it
    # had just started answered every request in nine milliseconds. The window sat on
    # "starting the gateway…" and then claimed A.R.S could not start.
    #
    # The shell reaches its own gateway over loopback no matter what interface that
    # gateway is exposed on, so this address is not configurable and must not become so.
    bind = host or os.environ.get("ARS_LISTEN_HOST", "127.0.0.1")
    base_url = f"http://127.0.0.1:{port}"

    box: dict[str, object] = {}

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        box["loop"] = loop
        try:
            import uvicorn

            try:
                from ars_gateway.app import app as gateway_app
            except Exception as exc:  # module not present yet, or failed to import
                box["import_error"] = f"{type(exc).__name__}: {exc}"
                log.error("could not import ars_gateway.app: %s", box["import_error"])
                return

            # Loopback unless the user has deliberately opened it. Opening it costs the
            # owner nothing — the window still connects over loopback — and is never done
            # on their behalf.
            config = uvicorn.Config(
                gateway_app,
                host=bind,
                port=port,
                log_level=os.environ.get("ARS_LOG_LEVEL", "info").lower(),
                timeout_graceful_shutdown=5,
                lifespan="on",
            )
            server = uvicorn.Server(config)
            box["server"] = server
            # wait_healthy() below polls the real /health endpoint over HTTP, which is
            # the actual, honest readiness signal (it only turns true once Ars.start()
            # has finished) — no separate internal "ready" bookkeeping needed here.
            loop.run_until_complete(server.serve())
        finally:
            with contextlib_suppress():
                loop.close()

    thread = threading.Thread(target=run, name="ars-gateway", daemon=True)
    thread.start()

    # Bounded wait for the server object/import outcome to exist at all (startup itself
    # is awaited above; this just guards against the thread dying before it gets there).
    deadline = time.monotonic() + 15
    while "server" not in box and "import_error" not in box and time.monotonic() < deadline:
        time.sleep(0.02)

    return GatewayHandle(
        port=port,
        base_url=base_url,
        thread=thread,
        _server=box.get("server"),
        _loop=box.get("loop"),
        import_error=box.get("import_error"),  # type: ignore[arg-type]
    )


def contextlib_suppress():
    import contextlib

    return contextlib.suppress(Exception)


def wait_healthy(handle: GatewayHandle, *, timeout: float = 25.0) -> bool:
    """Poll ``/health`` until the gateway answers ``{"ok": true}`` or ``timeout`` runs out.

    ``ok`` only turns true once ``Ars.start()`` has finished — memory store opened,
    grants store opened, and the Ollama warm-up attempted (which itself never blocks
    forever: it fails fast and records a note rather than hanging if Ollama is down).
    """
    if handle.import_error is not None:
        return False
    deadline = time.monotonic() + timeout
    url = f"{handle.base_url}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:  # noqa: S310 (loopback only)
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.1)
    return False
