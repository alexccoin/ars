#!/usr/bin/env bash
#
# Fetch the on-device voice models for A.R.S: openWakeWord, faster-whisper (large-v3-turbo)
# and Piper voices for English *and* Romanian.
#
# Nothing here is committed. models/ is gitignored and must stay that way — CLAUDE.md
# non-negotiable #8. This script is the only supported way to populate it.
#
# Total download: roughly 2.0-2.5 GB, dominated by whisper large-v3-turbo (~1.6 GB).
#
#   scripts/fetch_voice_models.sh              # everything
#   scripts/fetch_voice_models.sh wakeword tts # just those
#   ARS_ASR_MODEL=small scripts/fetch_voice_models.sh asr   # a smaller ASR for a phone
#
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS_DIR="${ARS_MODELS_DIR:-$ROOT/models}"
VENV_PY="${ARS_PYTHON:-$ROOT/.venv/bin/python}"

# Defaults mirror ars_core.VoiceConfig. Override via the environment; do not edit them here,
# or the code and the weights on disk will disagree.
ASR_MODEL="${ARS_ASR_MODEL:-large-v3-turbo}"
WAKEWORD="${ARS_WAKEWORD:-hey_ars}"
TTS_VOICE_EN="${ARS_TTS_VOICE_EN:-en_US-amy-medium}"
TTS_VOICE_RO="${ARS_TTS_VOICE_RO:-ro_RO-mihai-medium}"

PIPER_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main"
SILERO_URL="https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

fetch() {  # fetch <url> <destination>
  local url="$1" dest="$2"
  if [[ -s "$dest" ]]; then
    log "already present: ${dest#"$ROOT"/}"
    return 0
  fi
  mkdir -p "$(dirname "$dest")"
  log "downloading ${dest#"$ROOT"/}"
  # Download to a temp file first: a half-written model that looks present is worse than a
  # missing one, because it fails at the first turn instead of at setup.
  curl --fail --location --progress-bar --retry 3 --retry-delay 2 \
       --output "$dest.part" "$url" || { rm -f "$dest.part"; die "download failed: $url"; }
  mv "$dest.part" "$dest"
}

# ---------------------------------------------------------------------------- wakeword

fetch_wakeword() {
  log "wakeword -> $MODELS_DIR/wakeword"
  mkdir -p "$MODELS_DIR/wakeword"
  [[ -x "$VENV_PY" ]] || die "no venv python at $VENV_PY"
  "$VENV_PY" - "$MODELS_DIR/wakeword" <<'PY' || die "openwakeword not installed: uv pip install -e 'services/voice[wakeword]'"
import sys, shutil, pathlib
target = pathlib.Path(sys.argv[1])
import openwakeword.utils as utils
# Pulls the shared melspectrogram + embedding front end and the pretrained keyword models.
utils.download_models()
import openwakeword
src = pathlib.Path(openwakeword.__file__).parent / "resources" / "models"
for path in src.glob("*"):
    if path.suffix in {".onnx", ".tflite"}:
        shutil.copy2(path, target / path.name)
print(f"copied {len(list(target.glob('*.onnx')))} onnx model(s) to {target}")
PY

  if [[ ! -f "$MODELS_DIR/wakeword/$WAKEWORD.onnx" ]]; then
    warn "no model for the configured keyword '$WAKEWORD'."
    warn "openWakeWord ships 'hey_jarvis', 'alexa', 'hey_mycroft' and friends; a custom"
    warn "'hey_ars' has to be trained (see research/notes) or ARS_WAKEWORD set to a stock one."
    warn "Until then the pipeline runs with ARS_WAKE_BACKEND=mock."
  fi
}

# ---------------------------------------------------------------------------- ASR

fetch_asr() {
  log "faster-whisper $ASR_MODEL -> $MODELS_DIR/asr"
  mkdir -p "$MODELS_DIR/asr"
  [[ -x "$VENV_PY" ]] || die "no venv python at $VENV_PY"
  "$VENV_PY" - "$MODELS_DIR/asr" "$ASR_MODEL" <<'PY' || die "faster-whisper not installed: uv pip install -e 'services/voice[asr]'"
import sys, pathlib
target, model = pathlib.Path(sys.argv[1]), sys.argv[2]
from huggingface_hub import snapshot_download
repo = {
    "large-v3-turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "medium": "Systran/faster-whisper-medium",
    "small": "Systran/faster-whisper-small",
    "base": "Systran/faster-whisper-base",
}.get(model, f"Systran/faster-whisper-{model}")
path = snapshot_download(repo_id=repo, local_dir=str(target / model))
print(f"model at {path}")
PY
  echo
  log "ASR is int8 on CPU (CTranslate2 has no Metal backend). On an M-series laptop"
  log "large-v3-turbo int8 decodes a short utterance in roughly 150-400 ms; measure it with"
  log "  uv run ars-voice-latency"
  log "and compare against the 250 ms ASR row before assuming it fits."
}

# ---------------------------------------------------------------------------- TTS

fetch_tts() {
  log "piper voices -> $MODELS_DIR/tts"
  mkdir -p "$MODELS_DIR/tts"
  # Piper's layout is <lang_family>/<lang_REGION>/<name>/<quality>/<file>.
  for voice in "$TTS_VOICE_EN" "$TTS_VOICE_RO"; do
    local locale name quality family
    locale="${voice%%-*}"                 # en_US
    name="${voice#*-}"; name="${name%-*}" # amy
    quality="${voice##*-}"                # medium
    family="${locale%%_*}"                # en
    for suffix in ".onnx" ".onnx.json"; do
      fetch "$PIPER_BASE/$family/$locale/$name/$quality/$voice$suffix" \
            "$MODELS_DIR/tts/$voice$suffix"
    done
  done
  log "Romanian voice: $TTS_VOICE_RO — listen to it before shipping. A bilingual assistant"
  log "whose Romanian half sounds wrong is not bilingual in any sense the user cares about."
}

# ---------------------------------------------------------------------------- VAD

fetch_vad() {
  log "silero VAD -> $MODELS_DIR/vad"
  fetch "$SILERO_URL" "$MODELS_DIR/vad/silero_vad.onnx"
  log "optional: the energy VAD in ars_voice.vad.energy is the supported no-weights fallback."
}

# ---------------------------------------------------------------------------- main

main() {
  need curl
  local targets=("$@")
  if [[ ${#targets[@]} -eq 0 ]]; then
    targets=(wakeword vad asr tts)
  fi
  mkdir -p "$MODELS_DIR"
  for target in "${targets[@]}"; do
    case "$target" in
      wakeword) fetch_wakeword ;;
      asr)      fetch_asr ;;
      tts)      fetch_tts ;;
      vad)      fetch_vad ;;
      *)        die "unknown target '$target' (wakeword|vad|asr|tts)" ;;
    esac
  done

  log "done. models/ is gitignored — never commit weights."
  log "switch the pipeline onto them with:"
  echo "    export ARS_WAKE_BACKEND=openwakeword ARS_VAD_BACKEND=silero"
  echo "    export ARS_ASR_BACKEND=faster-whisper ARS_TTS_BACKEND=piper"
  log "then re-measure, in both languages:"
  echo "    uv run ars-voice-latency --turns 20"
  echo "    uv run ars-voice-wakeword-eval --write   # needs a real negative set first"
}

main "$@"
