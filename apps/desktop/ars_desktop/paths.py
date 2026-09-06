"""Where things live, in dev (running from the repo) and frozen (double-clicked .app).

The one rule that matters here: a PyInstaller bundle's working directory is whatever
Finder felt like (usually ``/``), and its own Resources folder is read-only and gets
replaced on every rebuild. Nothing that must persist (window geometry, the sqlite
memory store, the audit log, downloaded model weights) can live under either. It all
goes to the standard macOS per-user location instead.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

APP_SUPPORT_NAME = "A.R.S"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


@lru_cache(maxsize=1)
def app_support_dir() -> Path:
    """``~/Library/Application Support/A.R.S`` — created on first use.

    Holds the sqlite memory store, the guard audit log, the window-geometry file, and
    (only if the repo checkout is not found — see ``resolve_models_dir``) downloaded
    model weights. Never touched by the build; safe to delete for a clean slate.
    """
    base = Path.home() / "Library" / "Application Support" / APP_SUPPORT_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


@lru_cache(maxsize=1)
def bundle_resources_dir() -> Path | None:
    """The frozen app's read-only ``Contents/Resources`` (or ``sys._MEIPASS``), else None."""
    if not is_frozen():
        return None
    return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))


@lru_cache(maxsize=1)
def repo_root() -> Path | None:
    """The A.R.S source checkout, if we can find one.

    Three ways this can be known, in order:

    1. ``ARS_REPO_ROOT`` — set explicitly (e.g. for a dev launch of the frozen app).
    2. A hint file baked into the bundle at build time (``packaging/build_macos.sh``
       writes the path of the repo it was built from into ``Resources/repo_hint.txt``).
       This app is built by and for Alex on his own machine, not shrink-wrapped for
       strangers, so "the repo that built this app" is a real, useful answer to "where
       are the fetch scripts" for exactly the audience it ships to.
    3. Running un-frozen (``python -m ars_desktop`` from a checkout): walk up from this
       file until ``scripts/fetch_voice_models.sh`` is found.

    Returns None if none of these pan out — the app must still run, just without the
    one-click "fetch models" affordance (the first-run screen says so explicitly).
    """
    env = os.environ.get("ARS_REPO_ROOT")
    if env and (Path(env) / "scripts" / "fetch_voice_models.sh").exists():
        return Path(env)

    res = bundle_resources_dir()
    if res is not None:
        hint = res / "repo_hint.txt"
        if hint.exists():
            candidate = Path(hint.read_text(encoding="utf-8").strip())
            if (candidate / "scripts" / "fetch_voice_models.sh").exists():
                return candidate
        return None

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "scripts" / "fetch_voice_models.sh").exists():
            return parent
    return None


@lru_cache(maxsize=1)
def resolve_models_dir() -> Path:
    """Where model weights are read from.

    1. ``ARS_MODELS_DIR`` if set — the documented override
       (``scripts/fetch_voice_models.sh`` honours the same variable).
    2. The repo checkout's ``models/`` if one is known and already populated — running
       the packaged app on the same machine as a dev checkout should see the same
       weights, not ask to download them twice.
    3. ``~/Library/Application Support/A.R.S/models`` — the real destination for a
       double-clicked .app with no repo in sight.
    """
    env = os.environ.get("ARS_MODELS_DIR")
    if env:
        return Path(env)

    root = repo_root()
    if root is not None:
        candidate = root / "models"
        if candidate.exists() and any(candidate.iterdir()):
            return candidate

    return app_support_dir() / "models"


def user_env_override_file() -> Path:
    """A user-editable ``.env``-style file for the packaged app, read in addition to
    (and taking priority over) the built-in defaults set by ``ars_desktop.server``.

    Not shipped with the app and never written by it automatically — this is where
    Alex can point ``ARS_LLM_LOCAL_MODEL`` at a different tag, etc., without a repo
    checkout on hand.
    """
    return app_support_dir() / ".env"


def window_state_file() -> Path:
    return app_support_dir() / "window_state.json"


def log_file() -> Path:
    return app_support_dir() / "desktop.log"
