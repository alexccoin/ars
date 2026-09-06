"""Remember window size and position between launches.

pywebview has no built-in persistence for this, so it is done by hand: read a small
JSON file before creating the window, write it (debounced) on move/resize, and once
more on close so the final position is never lost to a debounce window.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass

from . import paths

log = logging.getLogger("ars.desktop.window_state")

DEFAULT_WIDTH = 1180
DEFAULT_HEIGHT = 800
MIN_WIDTH = 720
MIN_HEIGHT = 520


@dataclass
class WindowGeometry:
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    x: int | None = None
    y: int | None = None


def load() -> WindowGeometry:
    path = paths.window_state_file()
    if not path.exists():
        return WindowGeometry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return WindowGeometry(
            width=max(int(data.get("width", DEFAULT_WIDTH)), MIN_WIDTH),
            height=max(int(data.get("height", DEFAULT_HEIGHT)), MIN_HEIGHT),
            x=data.get("x"),
            y=data.get("y"),
        )
    except Exception:
        log.warning("could not read %s, using defaults", path, exc_info=True)
        return WindowGeometry()


def save(geometry: WindowGeometry) -> None:
    path = paths.window_state_file()
    try:
        path.write_text(json.dumps(asdict(geometry)), encoding="utf-8")
    except Exception:
        log.warning("could not write %s", path, exc_info=True)


class GeometryTracker:
    """Debounced move/resize -> disk, plus an unconditional flush on close."""

    def __init__(self, window, debounce_s: float = 0.6) -> None:  # noqa: ANN001
        self._window = window
        self._debounce_s = debounce_s
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _write_now(self) -> None:
        try:
            w = self._window
            geometry = WindowGeometry(
                width=int(w.width), height=int(w.height), x=int(w.x), y=int(w.y)
            )
            save(geometry)
        except Exception:
            log.debug("geometry write skipped", exc_info=True)

    def _on_change(self, *_args: object) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_s, self._write_now)
            self._timer.daemon = True
            self._timer.start()

    def flush(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._write_now()

    def attach(self) -> None:
        self._window.events.resized += self._on_change
        self._window.events.moved += self._on_change
