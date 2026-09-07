#!/usr/bin/env bash
# A.R.S bootstrap — installs the host tools A.R.S needs to run locally.
# Nothing here touches the network without telling you first. Re-runnable.
set -euo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

say "Checking host tools"
missing=()
have brew   || { echo "Homebrew is required: https://brew.sh"; exit 1; }
have ffmpeg || missing+=(ffmpeg)
have uv     || missing+=(uv)

if ((${#missing[@]})); then
  echo "Will install: ${missing[*]}"
  read -r -p "Proceed? [y/N] " ans
  [[ "$ans" == [yY]* ]] || { echo "Skipped."; exit 0; }
  brew install "${missing[@]}"
fi

say "Python environment (3.12)"
# `uv sync` alone is the whole story now: the root pyproject.toml depends on every
# application this repo ships (the gateway, the desktop shell, memory's embedding
# backend, voice's ASR/TTS/VAD/wakeword backends), so a bare sync leaves a working
# environment — no more hand `uv pip install`-ing individual packages or extras. Do not
# add per-package installs back here; anything installed outside pyproject.toml is
# exactly what the next `uv sync` will silently remove (see the comment at the top of
# the root pyproject.toml). `uv sync --dry-run` shows what a real sync would change.
uv sync

say "PyInstaller (only needed to build A.R.S.app — skip if you're not packaging)"
# --inexact matters: `--package ars-desktop` alone scopes the sync to *only* that
# member's own dependency closure and, being an exact sync by default, would uninstall
# every voice/memory backend and dev tool this same command just installed above.
# --inexact makes it additive instead of a second, narrower exact sync.
echo "Run: uv sync --package ars-desktop --group build --inexact"

say "Local reasoning model (optional but recommended)"
if have ollama; then
  echo "ollama present: $(ollama --version 2>/dev/null || true)"
else
  echo "Ollama is not installed. A.R.S runs its reasoning locally through it."
  read -r -p "Install ollama via brew? [y/N] " ans
  [[ "$ans" == [yY]* ]] && brew install ollama || echo "Skipping — set ARS_LLM_BACKEND=anthropic to use the cloud instead."
fi
if have ollama; then
  model="$(grep -E '^ARS_LLM_LOCAL_MODEL' .env 2>/dev/null | cut -d= -f2 || echo qwen3:14b)"
  echo "Pull the reasoning model with:  ollama pull ${model}"
fi

say "Voice models"
echo "Run scripts/fetch_voice_models.sh to download wakeword, ASR and EN/RO TTS voices."

say "Configuration"
[[ -f .env ]] || { cp .env.example .env; echo "Created .env from .env.example — review it."; }

say "Verify"
.venv/bin/python -m pytest tests/unit -q

cat <<'DONE'

A.R.S is bootstrapped.

  Next:
    1. Review .env
    2. ollama pull <model>                      (local reasoning)
    3. scripts/fetch_voice_models.sh            (voice, EN + RO)
    4. .venv/bin/python -m ars_cli              (talk to it)

  Nothing is granted access to your accounts until you grant it explicitly.
DONE
