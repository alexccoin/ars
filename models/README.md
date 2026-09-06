# models/

Model weights live here. **Nothing in this directory is committed** — see
[.gitignore](../.gitignore) and CLAUDE.md non-negotiable #8.

Populate it with:

```bash
scripts/fetch_voice_models.sh            # everything, ~1.6 GB
scripts/fetch_voice_models.sh tts vad    # just those
```

## What lands here

| Path | Contents | Size |
|---|---|---|
| `asr/large-v3-turbo/` | faster-whisper CTranslate2 checkpoint | ~1.5 GB |
| `tts/` | Piper voices — `en_US-amy-medium`, `ro_RO-mihai-medium` | ~122 MB |
| `vad/silero_vad.onnx` | Silero VAD | ~2 MB |
| `wakeword/` | openWakeWord keyword + shared front-end models | ~18 MB |

MLX whisper weights are **not** here: `mlx-whisper` caches them under `~/.cache/huggingface`
by repo id (`mlx-community/whisper-large-v3-turbo`).

## Wakeword

openWakeWord ships `alexa`, `hey_jarvis`, `hey_mycroft`, `hey_rhasspy`. There is **no
`hey_ars` model** — a custom one has to be trained. `ARS_WAKEWORD` defaults to
`hey_jarvis`; `alexa` is a poor choice in a house with an Echo.

The local reasoning model is managed by Ollama, not by this directory:
`ollama pull qwen3:14b`.
