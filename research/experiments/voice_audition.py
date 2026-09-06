"""Render an audition sheet of Piper voices and character effects to WAV files.

The owner has to LISTEN to these; no automated metric substitutes for it. Nothing here
opens an audio device - it only writes files under var/voice-auditions/ (gitignored).

    uv run python research/experiments/voice_audition.py

Requires the voices under models/tts (scripts/fetch_voice_models.sh).
"""
from __future__ import annotations

import json
import pathlib
import sys
from fractions import Fraction

import numpy as np
from scipy import signal as sg

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from character_dsp import SR, chain, f32_to_pcm, pcm_to_f32, process_stream, write_wav  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
MODELS = ROOT / "models" / "tts"
OUT = ROOT / "var" / "voice-auditions"

LINES = {
    "en": "Good morning. I found three new messages from the bank, and the first one looks urgent.",
    "ro": "Bună dimineața. Am găsit trei mesaje noi de la bancă, iar primul pare urgent.",
    "de": "Guten Morgen. Ich habe drei neue Nachrichten von der Bank gefunden, die erste wirkt dringend.",
}

# (voice, language, speaker_id or None, label)
ROSTER = [
    ("en_US-amy-medium", "en", None, "current-default"),
    ("en_GB-alan-medium", "en", None, "british-male"),
    ("en_US-hfc_female-medium", "en", None, "us-female-bright"),
    ("en_US-sam-medium", "en", None, "non-binary"),
    ("en_GB-cori-high", "en", None, "british-female-HIGH-slow"),
    ("en_GB-semaine-medium", "en", 1, "character-spike"),
    ("en_GB-semaine-medium", "en", 2, "character-obadiah"),
    ("en_GB-semaine-medium", "en", 3, "character-poppy"),
    ("en_GB-vctk-medium", "en", 0, "vctk-sample"),
    ("ro_RO-mihai-medium", "ro", None, "current-default"),
    ("de_DE-thorsten-medium", "de", None, "german-male"),
    ("de_DE-ramona-low", "de", None, "german-female"),
    ("de_DE-thorsten_emotional-medium", "de", 7, "emotion-whisper"),
    ("de_DE-thorsten_emotional-medium", "de", 5, "emotion-sleepy"),
]

PRESETS = ("robot_ring", "robot_dalek", "robot_vocoder", "alien_ring", "alien_swarm")


def synth16(voice: str, text: str, speaker: int | None, length_scale: float = 1.0,
            pretend_rate_ratio: float = 1.0) -> bytes:
    """Synthesise and land at the protocol rate, exactly as PiperTtsEngine does.

    `pretend_rate_ratio` k lies to the resampler about the source rate: pitch AND formants
    scale by k for free, because the resample happens anyway. Pass length_scale=k to cancel
    the duration change (residual ~10%, see research/voices.md).
    """
    from piper.config import SynthesisConfig
    from piper.voice import PiperVoice

    voice_obj = PiperVoice.load(str(MODELS / f"{voice}.onnx"))
    cfg = SynthesisConfig(length_scale=length_scale)
    if speaker is not None:
        cfg.speaker_id = speaker
    parts = []
    for chunk in voice_obj.synthesize(text, cfg):
        arr = getattr(chunk, "audio_float_array", None)
        if arr is None:
            arr = np.frombuffer(chunk.audio_int16_bytes, "<i2").astype(np.float32) / 32768
        parts.append(np.asarray(arr, np.float32))
    x = np.concatenate(parts)
    rate = int(round(voice_obj.config.sample_rate * pretend_rate_ratio))
    frac = Fraction(SR, rate).limit_denominator(4000)
    y = sg.resample_poly(x, frac.numerator, frac.denominator).astype(np.float32)
    return f32_to_pcm(y)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for voice, lang, speaker, label in ROSTER:
        if not (MODELS / f"{voice}.onnx").is_file():
            print(f"skip (not downloaded): {voice}")
            continue
        pcm = synth16(voice, LINES[lang], speaker)
        sid = "" if speaker is None else f".s{speaker}"
        name = f"{lang}.{voice}{sid}.{label}.wav"
        write_wav(OUT / name, pcm)
        manifest.append({"file": name, "voice": voice, "language": lang, "speaker": speaker})
        print(f"wrote {name}")

    # Character effects, on all three languages, over the default voice of each.
    for voice, lang in [("en_US-amy-medium", "en"), ("ro_RO-mihai-medium", "ro"),
                        ("de_DE-thorsten-medium", "de")]:
        if not (MODELS / f"{voice}.onnx").is_file():
            continue
        dry = synth16(voice, LINES[lang], None)
        for preset in PRESETS:
            wet, _ = process_stream(dry, preset)
            name = f"fx.{lang}.{preset}.wav"
            write_wav(OUT / name, wet)
            manifest.append({"file": name, "voice": voice, "language": lang, "preset": preset})
            print(f"wrote {name}")

    # The only route to a second Romanian timbre without training a voice: shift pitch and
    # formants together by lying about the source rate. k < 1 = bigger/deeper, k > 1 = smaller.
    for k in (0.85, 1.18, 1.30):
        pcm = synth16("ro_RO-mihai-medium", LINES["ro"], None,
                      length_scale=k, pretend_rate_ratio=k)
        name = f"ro.mihai.formant_k{k:.2f}.wav"
        write_wav(OUT / name, pcm)
        manifest.append({"file": name, "voice": "ro_RO-mihai-medium", "language": "ro",
                         "formant_ratio": k})
        print(f"wrote {name}")

    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\n{len(manifest)} files under {OUT}")
    print("Listen to them yourself; this script deliberately never opens an output device.")


if __name__ == "__main__":
    main()
