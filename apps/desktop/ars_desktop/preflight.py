"""What's missing, in words a person can act on.

Two things the app cannot bundle and cannot function fully without: the voice model
weights (~1.6 GB, gitignored, fetched by ``scripts/fetch_voice_models.sh``) and Ollama
(the local reasoning backend, a separate process A.R.S talks to over HTTP). Neither
missing-ness should crash the app — the gateway already degrades gracefully on a
missing Ollama (``ars_gateway.app`` answers from documents/memory and says so). This
module is the desktop shell's half: detect, describe, and offer the fix, before the
window ever opens on a confusing state.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

# Mirrors models/README.md. Checked as "does at least one expected file exist per
# category" rather than every file, because a partial-but-usable set (e.g. only the
# English Piper voice) is a real and useful state, not a broken one.
_MODEL_MANIFEST: dict[str, tuple[str, ...]] = {
    "asr": ("asr/large-v3-turbo/model.bin",),
    "tts": ("tts/en_US-amy-medium.onnx", "tts/ro_RO-mihai-medium.onnx"),
    "vad": ("vad/silero_vad.onnx",),
    "wakeword": ("wakeword/hey_jarvis_v0.1.onnx", "wakeword/alexa_v0.1.onnx"),
}


@dataclass
class ModelStatus:
    models_dir: Path
    present: dict[str, bool] = field(default_factory=dict)

    @property
    def all_present(self) -> bool:
        return all(self.present.values())

    @property
    def any_present(self) -> bool:
        return any(self.present.values())

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(k for k, v in self.present.items() if not v)


def check_models() -> ModelStatus:
    models_dir = paths.resolve_models_dir()
    present = {
        category: any((models_dir / rel).exists() for rel in rels)
        for category, rels in _MODEL_MANIFEST.items()
    }
    return ModelStatus(models_dir=models_dir, present=present)


@dataclass
class OllamaStatus:
    installed: bool
    binary: str | None
    running: bool
    version: str | None = None


def check_ollama(host: str = "127.0.0.1", port: int = 11434, timeout: float = 0.35) -> OllamaStatus:
    binary = shutil.which("ollama")
    version: str | None = None
    if binary:
        try:
            out = subprocess.run(
                [binary, "--version"], capture_output=True, text=True, timeout=2
            )
            version = out.stdout.strip() or None
        except Exception:
            version = None

    running = False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            running = True
    except OSError:
        running = False

    return OllamaStatus(installed=binary is not None, binary=binary, running=running, version=version)


def fetch_models_command(status: ModelStatus) -> str | None:
    """The exact command that fixes a missing-models state, or None if we don't know
    where the repo is (frozen app with no known checkout — the UI says so instead)."""
    root = paths.repo_root()
    if root is None:
        return None
    missing = status.missing or tuple(_MODEL_MANIFEST)
    script = root / "scripts" / "fetch_voice_models.sh"
    return f'cd {shquote(str(root))} && ARS_MODELS_DIR={shquote(str(status.models_dir))} {shquote(str(script))} {" ".join(missing)}'


def shquote(s: str) -> str:
    if not s or any(c in s for c in " \t\"'$`\\"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


def ollama_advice(status: OllamaStatus) -> str:
    if not status.installed:
        return (
            "Ollama is not installed. A.R.S uses it to run the local reasoning model "
            "(qwen3:14b) on this Mac. Install it with:\n\n    brew install ollama\n\n"
            "then pull the model:\n\n    ollama pull qwen3:14b\n\n"
            "Without it, A.R.S still answers from your own documents and memory — "
            "just not with the full model."
        )
    if not status.running:
        return (
            "Ollama is installed but is not running. Start it with:\n\n    ollama serve\n\n"
            "or open the Ollama app from Applications. A.R.S will pick it up automatically "
            "on the next question — no restart needed."
        )
    return f"Ollama is running ({status.version or 'version unknown'})."
