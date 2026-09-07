# PyInstaller spec for A.R.S.app — macOS, onedir + windowed (no terminal).
#
# Build with:  scripts do not invoke pyinstaller directly with CLI flags because the
# things that need control here (a custom Info.plist key, excluding models/, a onedir
# rather than onefile layout so startup isn't paying an unzip tax every launch) are
# only reachable through the BUNDLE()/EXE() spec API, not the CLI. Run this file, don't
# hand-edit dist/ or build/ around it — see packaging/build_macos.sh.
#
# What is deliberately NOT here: model weights. Nothing under models/ is referenced by
# any `datas`/`binaries` entry below, and PyInstaller only ever bundles what it can
# trace from imports plus what is explicitly listed here — so weights (gitignored,
# fetched by scripts/fetch_voice_models.sh, CLAUDE.md non-negotiable #8) never enter
# the bundle unless someone adds them by hand later. `packaging/build_macos.sh` also
# scans the finished .app for weight-shaped files as a second check.
from __future__ import annotations

import pathlib
import tempfile

from PyInstaller.utils.hooks import collect_all

block_cipher = None

# PyInstaller execs spec files with `exec()`, not as a normal module import, so
# `__file__` is not defined here — it injects `SPECPATH` (this file's directory)
# instead.
REPO_ROOT = pathlib.Path(SPECPATH).resolve().parent  # noqa: F821
ENTRY = REPO_ROOT / "packaging" / "macos_entry.py"

# ---------------------------------------------------------------------------------
# repo_hint.txt — see apps/desktop/ars_desktop/paths.py:repo_root(). Written at build
# time (not by the app) so a packaged app built from this checkout can find
# scripts/fetch_voice_models.sh without asking. Regenerated fresh on every build so it
# can never point at a checkout that has moved or been deleted since.
# ---------------------------------------------------------------------------------
_hint_dir = pathlib.Path(tempfile.mkdtemp(prefix="ars-repo-hint-"))
_hint_file = _hint_dir / "repo_hint.txt"
_hint_file.write_text(str(REPO_ROOT) + "\n", encoding="utf-8")

# Packages whose payload (native shared libraries, bundled model/data files, or
# submodules PyInstaller's static import trace cannot see because they load by
# manifest/plugin rather than a literal `import`) need to be pulled in explicitly.
# Everything else (mlx_whisper, faster_whisper, piper, sentence_transformers,
# sqlite_vec) is reached fine by following the literal `import` statements inside
# services/voice and services/memory's backend modules — collect_all is reserved for
# packages actually known to carry non-code payload.
COLLECT_ALL = [
    "mlx",  # Metal shader binaries
    "onnxruntime",  # bundled shared library
    "piper",  # ships espeak-ng-data
    "openwakeword",  # ships its .tflite/.onnx wakeword models as package data
    "sounddevice",  # bundles a portaudio dylib
    "silero_vad",  # ships its packaged onnx model
]

# Built up *before* Analysis() runs, not appended to a.datas/a.binaries afterwards:
# Analysis() normalizes its own datas/binaries into internal 3-tuple TOC entries, while
# collect_all() returns plain 2-tuple (src, dest) pairs in the format Analysis() itself
# expects as input — mixing the two by appending post-hoc breaks COLLECT()'s TOC
# normalization ("not enough values to unpack").
extra_datas = [(str(_hint_file), ".")]
extra_binaries = []
extra_hiddenimports = []
for _pkg in COLLECT_ALL:
    try:
        _datas, _binaries, _hiddenimports = collect_all(_pkg)
    except Exception:
        # Optional backend not installed in this build environment (e.g. built without
        # the wakeword extra) — degrade to the mock/null engine at runtime, same as an
        # un-fetched model does; do not fail the build over an optional import.
        continue
    extra_datas += _datas
    extra_binaries += _binaries
    extra_hiddenimports += _hiddenimports

a = Analysis(
    [str(ENTRY)],
    pathex=[str(REPO_ROOT)],
    binaries=extra_binaries,
    datas=extra_datas,
    hiddenimports=extra_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "mypy",
        "ruff",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="A.R.S",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed — no terminal on launch
    disable_windowed_traceback=False,
    target_arch=None,  # build for the arch running PyInstaller (arm64 on this repo's target hardware)
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="A.R.S",
)

app = BUNDLE(
    coll,
    name="A.R.S.app",
    icon=None,  # no icon asset exists yet anywhere in the repo; falls back to the default
    bundle_identifier="com.ars-project.desktop",
    info_plist={
        "CFBundleName": "A.R.S",
        "CFBundleDisplayName": "A.R.S",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "LSMinimumSystemVersion": "13.5",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.productivity",
        # The app opens the microphone for wakeword/ASR. Without this key, an
        # unbundled `python -m ars_desktop` process gets no system permission prompt
        # at all — macOS silently denies mic access to processes that don't carry it,
        # which was a live suspect in a voice bug: a packaged .app is what makes the
        # permission prompt (and TCC's per-app grant) exist in the first place.
        "NSMicrophoneUsageDescription": (
            "A.R.S listens for your voice to answer you and act on requests you "
            "approve. Audio is processed locally on this Mac; nothing is sent "
            "anywhere without your explicit permission."
        ),
    },
)
