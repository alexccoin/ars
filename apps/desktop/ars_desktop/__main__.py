"""A.R.S desktop — entry point.

    .venv/bin/python -m ars_desktop        # dev
    open dist/A.R.S.app                    # packaged

Sequence: open a native window immediately with a splash screen (never a blank/white
WKWebView — that *is* the flash this avoids), start the gateway on a background
thread, poll it for real health, then either navigate the same window onto it or show
a diagnostic screen that explains what is missing and how to fix it. On window close,
shut the gateway down gracefully (drain, don't kill) before the process exits, so
nothing is ever left running.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import webview
from webview.menu import Menu, MenuAction, MenuSeparator

from . import preflight, screens
from .paths import app_support_dir, log_file, repo_root
from .server import GatewayHandle, start_gateway, wait_healthy
from .window_state import DEFAULT_HEIGHT, DEFAULT_WIDTH, MIN_HEIGHT, MIN_WIDTH, GeometryTracker, load

log = logging.getLogger("ars.desktop")

BOOT_TIMEOUT_S = 90.0
"""How long to wait for the gateway before showing the diagnostic screen.

Was 25 s, which was generous when the gateway opened two SQLite files and was
comfortably wrong once it also loaded an embedding model. The window then showed "A.R.S
could not start" over a process that started fine moments later — the worst kind of error
message, because it is confidently false and the user has no way to tell.

The gateway now reports ready before warming its models, so this is a backstop against a
genuinely dead process rather than a race against startup work. Long, because the cost of
waiting is a splash screen and the cost of giving up early is a lie."""
WINDOW_TITLE = "A.R.S"


def _setup_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(log_file(), encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=os.environ.get("ARS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )


class Api:
    """The bridge the diagnostic screen's buttons call into (``pywebview.api.*``)."""

    def __init__(self, shell: "DesktopShell") -> None:
        self._shell = shell

    def retry(self) -> None:
        self._shell.boot_or_diagnose(rerender=True)

    def fetch_models(self) -> None:
        self._shell.fetch_models()

    def quit(self) -> None:
        window = webview.active_window() or (webview.windows[0] if webview.windows else None)
        if window:
            window.destroy()


class DesktopShell:
    def __init__(self) -> None:
        self.window: webview.Window | None = None
        self.gateway: GatewayHandle | None = None
        self.geometry_tracker: GeometryTracker | None = None
        self._boot_lock = threading.Lock()
        self._shut_down = False

    # ---- window -----------------------------------------------------------------

    def build_window(self) -> webview.Window:
        state = load()
        window = webview.create_window(
            WINDOW_TITLE,
            html=screens.splash_html("starting the gateway…"),
            width=state.width,
            height=state.height,
            x=state.x,
            y=state.y,
            min_size=(MIN_WIDTH, MIN_HEIGHT),
            resizable=True,
            fullscreen=False,
            background_color="#05070d",
            confirm_close=False,
            js_api=Api(self),
        )
        window.events.closing += self._on_closing
        window.events.loaded += self._on_first_loaded
        self.window = window
        return window

    def _on_first_loaded(self, *_args: object) -> None:
        # Attach geometry tracking once, after the first page load (window fully
        # realised on macOS by then); avoid re-attaching on every subsequent
        # load_html/load_url.
        if self.geometry_tracker is None and self.window is not None:
            self.geometry_tracker = GeometryTracker(self.window)
            self.geometry_tracker.attach()

    def _on_closing(self, *_args: object) -> None:
        if self.geometry_tracker is not None:
            self.geometry_tracker.flush()
        self.shutdown()

    # ---- boot ---------------------------------------------------------------------

    def start(self) -> None:
        window = self.build_window()
        # webview.start() blocks until the window is closed; boot happens on a
        # daemon thread so the native event loop can come up and paint the splash
        # immediately instead of waiting on network/process I/O first.
        threading.Thread(target=self.boot_or_diagnose, daemon=True, name="ars-boot").start()
        webview.start(menu=self._menu(), gui="cocoa")

    def _menu(self) -> list[Menu]:
        return [
            Menu("__app__", [
                MenuAction("Open Data Folder", self._open_data_folder),
                MenuAction("Fetch Voice Models…", self.fetch_models),
                MenuSeparator(),
                MenuAction("View Logs", self._open_logs),
            ]),
        ]

    def _open_data_folder(self) -> None:
        subprocess.run(["open", str(app_support_dir())], check=False)

    def _open_logs(self) -> None:
        webbrowser.open(log_file().as_uri())

    def boot_or_diagnose(self, *, rerender: bool = False) -> None:
        if not self._boot_lock.acquire(blocking=False):
            # A boot is already in flight. Retry used to return silently here, so a user
            # looking at a stuck window pressed the only button on it and watched nothing
            # happen — twice, in Alex's case. Say so.
            log.info("boot already in progress; ignoring retry")
            if rerender and self.window is not None:
                self.window.evaluate_js(
                    "document.body && document.body.setAttribute('data-retrying','1')"
                )
            return
        try:
            if self.gateway is None:
                log.info("starting gateway…")
                self.gateway = start_gateway()
            healthy = wait_healthy(self.gateway, timeout=BOOT_TIMEOUT_S if not rerender else 6.0)
            if healthy:
                log.info("gateway healthy on port %s", self.gateway.port)
                if self.window is not None:
                    self.window.load_url(self.gateway.base_url + "/")
                return
            self._show_diagnostics()
        finally:
            self._boot_lock.release()

    def _show_diagnostics(self) -> None:
        models = preflight.check_models()
        ollama = preflight.check_ollama()
        fetch_cmd = preflight.fetch_models_command(models)
        html = screens.diagnostic_html(
            gateway_error=self.gateway.import_error if self.gateway else "gateway not started",
            models=models,
            ollama=ollama,
            fetch_cmd=fetch_cmd,
            port=self.gateway.port if self.gateway else None,
        )
        if self.window is not None:
            self.window.load_html(html)

    def fetch_models(self) -> None:
        models = preflight.check_models()
        fetch_cmd = preflight.fetch_models_command(models)
        if fetch_cmd is None:
            self._show_diagnostics()
            return
        script_path = app_support_dir() / "fetch_models.command"
        script_path.write_text(
            "#!/bin/bash\nset -e\n"
            f"echo 'A.R.S — fetching voice models into {models.models_dir}'\n"
            f"{fetch_cmd}\n"
            "echo\necho 'Done. You can close this window and reopen A.R.S.'\n"
            "read -r -p 'Press Enter to close... '\n",
            encoding="utf-8",
        )
        script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        subprocess.run(["open", str(script_path)], check=False)
        if self.window is not None:
            self.window.load_html(screens.fetch_started_html(fetch_cmd))

    # ---- shutdown -------------------------------------------------------------

    def shutdown(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        if self.gateway is not None:
            self.gateway.stop()


def main() -> None:
    _setup_logging()
    log.info("A.R.S desktop starting (repo_root=%s)", repo_root())
    DesktopShell().start()
    log.info("A.R.S desktop exited cleanly")


if __name__ == "__main__":
    main()
