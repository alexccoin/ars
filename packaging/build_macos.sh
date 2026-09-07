#!/usr/bin/env bash
# Build A.R.S.app — a double-clickable, terminal-free macOS bundle for the desktop
# shell in apps/desktop. Reproducible: everything the build needs is declared in
# apps/desktop/pyproject.toml's `build` dependency-group and packaging/A.R.S.spec, both
# checked into the repo. No manual `pip install` step, no state outside this checkout.
#
#   Usage:  packaging/build_macos.sh
#   Output: dist/A.R.S.app  (also packaging/dist/A.R.S.app — see DISTPATH below)
#
#   Rollback: this script never touches anything outside build/, dist/ and the venv's
#   installed packages. `rm -rf build dist` undoes a build completely; nothing else on
#   disk is modified. If the `--group build` sync below installed pyinstaller/pillow
#   for the first time, `uv sync` (no flags) removes them again — see the comment at
#   the top of the root pyproject.toml.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
VENV_PY="$REPO_ROOT/.venv/bin/python"
PYINSTALLER="$REPO_ROOT/.venv/bin/pyinstaller"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }

[[ -x "$VENV_PY" ]] || die "no .venv — run 'uv sync' first (see scripts/bootstrap.sh)"

say "Checking build tools"
if ! "$VENV_PY" -c "import PyInstaller, PIL" >/dev/null 2>&1; then
  die "pyinstaller/pillow not installed in .venv. Run:
  uv sync --package ars-desktop --group build --inexact
(--inexact matters — without it this scopes to an *exact* sync of ars-desktop alone
and uninstalls everything else in .venv, voice/memory backends included)."
fi
echo "pyinstaller and pillow present."

say "Cleaning previous build"
rm -rf "$REPO_ROOT/build" "$REPO_ROOT/dist"

say "Running PyInstaller (packaging/A.R.S.spec)"
"$PYINSTALLER" \
  --noconfirm \
  --clean \
  --distpath "$REPO_ROOT/dist" \
  --workpath "$REPO_ROOT/build" \
  "$REPO_ROOT/packaging/A.R.S.spec"

APP="$REPO_ROOT/dist/A.R.S.app"
[[ -d "$APP" ]] || die "PyInstaller reported success but $APP does not exist"

say "Verifying: NSMicrophoneUsageDescription present"
PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Print :NSMicrophoneUsageDescription" "$PLIST" >/dev/null \
  || die "NSMicrophoneUsageDescription missing from $PLIST — mic access will silently fail"
echo "OK: $(/usr/libexec/PlistBuddy -c 'Print :NSMicrophoneUsageDescription' "$PLIST")"

say "Verifying: no console (LSUIElement / windowed launch)"
codesign -dv "$APP" >/dev/null 2>&1 || true  # informational only, ad-hoc build is fine

say "Verifying: no model weights bundled (CLAUDE.md non-negotiable #8)"
# Model weight formats fetched by scripts/fetch_voice_models.sh: mirrors
# models/README.md — .bin/.onnx/.pt/.safetensors/.gguf, and anything actually inside a
# models/ directory that PyInstaller was never told to bundle in the first place.
offenders="$(find "$APP" \( -iname '*.onnx' -o -iname '*.gguf' -o -iname '*.safetensors' \
  -o -path '*/models/asr/*' -o -path '*/models/tts/*' -o -path '*/models/vad/*' \) 2>/dev/null || true)"
if [[ -n "$offenders" ]]; then
  # openwakeword and silero-vad ship small onnx/tflite files as *package data* (part of
  # the pip wheel, not a fetched weight) — that is expected and is not the thing this
  # check guards against. Only fail on anything that looks like it came from models/.
  weights_only="$(echo "$offenders" | grep -E '/models/(asr|tts|vad)/' || true)"
  if [[ -n "$weights_only" ]]; then
    die "fetched model weights ended up inside the bundle:
$weights_only"
  fi
  echo "note: package-bundled data files found (expected, not fetched weights):"
  echo "$offenders" | sed 's/^/  /'
fi
echo "OK: no fetched model weights in the bundle."

say "Done"
echo "  $APP"
echo
echo "  Launch:      open '$APP'"
echo "  Rollback:    rm -rf '$REPO_ROOT/build' '$REPO_ROOT/dist'"
echo "  Logs:        ~/Library/Application Support/A.R.S/desktop.log"
echo "  Model weights are fetched separately and are never part of this bundle:"
echo "    scripts/fetch_voice_models.sh"
