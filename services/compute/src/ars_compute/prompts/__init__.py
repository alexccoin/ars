"""Loader for the versioned prompt assets in this directory.

There are no prompt strings in A.R.S's Python source. Every system prompt, quarantine
frame, memory frame and spoken filler is a file next to this module and listed in
`manifest.toml`. The loader hashes each file at load time and exposes the hash, so a
turn's telemetry can name the exact bytes that produced it.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ars_protocol import Language

from ..errors import PromptAssetMissing

_DIR = Path(__file__).parent


def _version_key(version: str) -> tuple[int, str]:
    """Order "v2" after "v10" correctly, and never crash on an unconventional name."""
    digits = "".join(c for c in version if c.isdigit())
    return (int(digits) if digits else 0, version)


@dataclass(frozen=True)
class PromptAsset:
    id: str
    version: str
    language: Language | None
    path: str
    text: str
    sha256: str

    @property
    def ref(self) -> str:
        """Short, log-safe identity: `system/v1/ro@3f9a1c2d`."""
        lang = self.language.value if self.language else "any"
        return f"{self.id}/{self.version}/{lang}@{self.sha256[:8]}"

    def render(self, **fields: object) -> str:
        return self.text.format(**fields)


@dataclass(frozen=True)
class PromptLibrary:
    registry_version: str
    assets: tuple[PromptAsset, ...]

    def get(self, asset_id: str, language: Language | None = None,
            version: str | None = None) -> PromptAsset:
        """The newest version of an asset, or a named one.

        Versions are added as new files and never edited in place, so "newest" is the only
        sensible default: a caller that wanted v1's exact bytes was recording an eval and
        knows to ask for them by name. Resolving to the newest is what lets a prompt be
        superseded — as `system` was when a third language made its opening sentence
        wrong — without every call site learning the new number.
        """
        candidates = [
            a for a in self.assets if a.id == asset_id and a.language == language
        ] or [
            # Language-neutral assets are registered with language=None; fall back to
            # those before failing, so a caller can ask for ("fillers", RO) and get the
            # shared file.
            a for a in self.assets if a.id == asset_id and a.language is None
        ]
        if version is not None:
            candidates = [a for a in candidates if a.version == version]
        if not candidates:
            raise PromptAssetMissing(asset_id, language)
        return max(candidates, key=lambda a: _version_key(a.version))

    def refs(self) -> tuple[str, ...]:
        return tuple(sorted(a.ref for a in self.assets))


def _load_manifest() -> PromptLibrary:
    manifest = tomllib.loads((_DIR / "manifest.toml").read_text(encoding="utf-8"))
    assets: list[PromptAsset] = []
    for entry in manifest["asset"]:
        path = _DIR / entry["path"]
        text = path.read_text(encoding="utf-8")
        lang_raw = entry.get("language")
        assets.append(
            PromptAsset(
                id=entry["id"],
                version=entry["version"],
                language=Language(lang_raw) if lang_raw else None,
                path=entry["path"],
                text=text,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )
    return PromptLibrary(registry_version=manifest["registry_version"], assets=tuple(assets))


@lru_cache(maxsize=1)
def library() -> PromptLibrary:
    """Loaded once per process. Prompts are immutable at runtime by design: a prompt that
    can change mid-session makes the eval results meaningless."""
    return _load_manifest()


@lru_cache(maxsize=1)
def fillers() -> dict:
    return tomllib.loads((_DIR / "fillers.v1.toml").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def notices() -> dict:
    return tomllib.loads((_DIR / "notices.v1.toml").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def guard_summaries() -> dict:
    return tomllib.loads((_DIR / "guard_summary.v1.toml").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def observation_templates() -> dict:
    return tomllib.loads((_DIR / "observations.v1.toml").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def observation_signals() -> dict:
    return tomllib.loads((_DIR / "observation_signals.v1.toml").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def tool_type_map(version: str = "v1") -> dict:
    path = _DIR / "tool_schemas" / version / "type_map.json"
    return json.loads(path.read_text(encoding="utf-8"))


def system_prompt(language: Language) -> PromptAsset:
    return library().get("system", language)


def quarantine_frame(language: Language) -> PromptAsset:
    return library().get("quarantine", language)


__all__ = [
    "PromptAsset", "PromptLibrary", "fillers", "guard_summaries", "library", "notices",
    "observation_signals", "observation_templates", "quarantine_frame", "system_prompt",
    "tool_type_map",
]
