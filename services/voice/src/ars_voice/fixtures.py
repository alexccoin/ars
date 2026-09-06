"""Fixture generation for endpointing and wakeword evaluation.

**These fixtures are synthetic and labelled as such everywhere they are used.** They are
speech-*shaped* noise with controlled onsets, offsets, pauses and levels — enough to tune
and regression-test the timing logic (endpointing, minimum-utterance guard, hesitation
grace, refractory period), and *not* enough to make a claim about recognition accuracy or
about false accepts on real household audio.

They exist because the alternative is tuning by feel, which is not tuning. Real recordings
go in the same directory with the same manifest schema, and `synthetic: false`; the eval
scripts do not care which kind they get, so the day real audio lands the numbers are
directly comparable.

Every waveform is deterministic given the manifest, so a before/after comparison is a
comparison of the code and nothing else.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ars_protocol import Language

from .audio.sources import write_wav
from .audio.synth import (
    formants_for,
    keyword_marker_pcm,
    mix,
    room_noise_pcm,
    speech_like_pcm,
)

DEFAULT_DIR = Path("./data/fixtures")


@dataclass
class Segment:
    """One labelled stretch of a fixture."""

    kind: str  # "speech" | "silence" | "transient" | "keyword"
    duration_ms: int
    text: str = ""
    """What a perfect ASR would have produced for this stretch, used to drive the
    endpointer's trailing-hesitation logic exactly as a real partial would."""


@dataclass
class FixtureSpec:
    name: str
    language: Language
    segments: list[Segment]
    noise_floor_dbfs: float = -62.0
    speech_dbfs: float = -22.0
    description: str = ""
    synthetic: bool = True

    @property
    def speech_end_ms(self) -> int:
        """End of the last speech segment: the ground truth an endpointer is judged against.

        Endpointing before this is a truncation — the user was cut off. Endpointing after is
        latency. The whole tuning exercise is trading the second to avoid the first.
        """
        cursor = 0
        last = 0
        for segment in self.segments:
            cursor += segment.duration_ms
            if segment.kind == "speech":
                last = cursor
        return last

    @property
    def total_ms(self) -> int:
        return sum(s.duration_ms for s in self.segments)

    def transcript_at(self, position_ms: float) -> str:
        """What the running partial transcript would say at this point in the fixture."""
        cursor = 0
        words: list[str] = []
        for segment in self.segments:
            if segment.text and cursor + segment.duration_ms <= position_ms + 1:
                words.append(segment.text)
            cursor += segment.duration_ms
        return " ".join(words)

    def render(self, seed: int = 0) -> bytes:
        pcm = bytearray()
        for index, segment in enumerate(self.segments):
            if segment.kind == "speech":
                layer = speech_like_pcm(
                    segment.duration_ms,
                    amplitude_dbfs=self.speech_dbfs,
                    formants=formants_for(self.language.value),
                    f0_hz=112.0 if self.language is Language.RO else 124.0,
                    seed=seed + index,
                )
            elif segment.kind == "keyword":
                layer = keyword_marker_pcm(segment.duration_ms, seed=seed + 300 + index)
            elif segment.kind == "transient":
                # A cough/door/keyboard: short, loud, no syllable structure. The minimum
                # utterance guard exists for exactly this.
                layer = speech_like_pcm(
                    segment.duration_ms,
                    amplitude_dbfs=self.speech_dbfs + 3,
                    syllable_rate_hz=1.0,
                    f0_hz=90.0,
                    seed=seed + 100 + index,
                )
            else:
                layer = b"\x00" * (segment.duration_ms * 32)
            noise = room_noise_pcm(
                segment.duration_ms, level_dbfs=self.noise_floor_dbfs, seed=seed + 500 + index
            )
            pcm += mix(layer, noise)
        return bytes(pcm)


def endpointing_specs() -> list[FixtureSpec]:
    """The cases endpointing is actually judged on.

    Chosen so that the two failure modes pull in opposite directions: half of these punish
    a short silence window (truncation) and half punish a long one (latency). A set that
    only contains one kind can be 'passed' by moving the number in one direction forever.
    """
    return [
        FixtureSpec(
            name="en_short_command",
            language=Language.EN,
            description="Plain command, clean stop. Pure latency case.",
            segments=[
                Segment("silence", 300),
                Segment("speech", 1_100, "turn off the kitchen light"),
                Segment("silence", 2_000),
            ],
        ),
        FixtureSpec(
            name="en_mid_sentence_pause",
            language=Language.EN,
            description="350 ms breath in the middle. Endpointing here truncates the user.",
            segments=[
                Segment("silence", 300),
                Segment("speech", 900, "send an email to Andrei"),
                Segment("silence", 350),
                Segment("speech", 1_000, "about tomorrow's invoice"),
                Segment("silence", 2_000),
            ],
        ),
        FixtureSpec(
            name="en_trailing_hesitation",
            language=Language.EN,
            description="Trails off on 'and', thinks for 620 ms, continues. The hard case.",
            segments=[
                Segment("silence", 300),
                Segment("speech", 1_000, "remind me to call the plumber and"),
                Segment("silence", 620),
                Segment("speech", 800, "also book the car service"),
                Segment("silence", 2_000),
            ],
        ),
        FixtureSpec(
            name="en_long_thought",
            language=Language.EN,
            description="Two long clauses with a 500 ms gap.",
            segments=[
                Segment("silence", 300),
                Segment("speech", 1_800, "i want you to look at the calendar for next week"),
                Segment("silence", 500),
                Segment("speech", 1_400, "and tell me which mornings are free"),
                Segment("silence", 2_000),
            ],
        ),
        FixtureSpec(
            name="en_two_word",
            language=Language.EN,
            description="Very short utterance. Must still endpoint, must not be swallowed.",
            segments=[
                Segment("silence", 300),
                Segment("speech", 520, "stop that"),
                Segment("silence", 2_000),
            ],
        ),
        FixtureSpec(
            name="en_cough_then_command",
            language=Language.EN,
            description="Transient before the utterance. Minimum-utterance guard case.",
            segments=[
                Segment("silence", 300),
                Segment("transient", 120),
                Segment("silence", 700),
                Segment("speech", 1_100, "play something quiet"),
                Segment("silence", 2_000),
            ],
        ),
        FixtureSpec(
            name="en_noisy_kitchen",
            language=Language.EN,
            description="Noise floor 17 dB higher. Fixed-threshold VAD fails this one.",
            noise_floor_dbfs=-45.0,
            segments=[
                Segment("silence", 600),
                Segment("speech", 1_200, "set a timer for ten minutes"),
                Segment("silence", 2_000),
            ],
        ),
        FixtureSpec(
            name="ro_short_command",
            language=Language.RO,
            description="Comandă scurtă, oprire clară.",
            segments=[
                Segment("silence", 300),
                Segment("speech", 1_100, "stinge lumina din bucătărie"),
                Segment("silence", 2_000),
            ],
        ),
        FixtureSpec(
            name="ro_mid_sentence_pause",
            language=Language.RO,
            description="Pauză de respirație de 350 ms în mijloc.",
            segments=[
                Segment("silence", 300),
                Segment("speech", 950, "trimite un email lui Andrei"),
                Segment("silence", 350),
                Segment("speech", 1_050, "despre factura de mâine"),
                Segment("silence", 2_000),
            ],
        ),
        FixtureSpec(
            name="ro_trailing_hesitation",
            language=Language.RO,
            description="Se oprește pe 'și', se gândește 600 ms, continuă.",
            segments=[
                Segment("silence", 300),
                Segment("speech", 1_000, "adaugă pâine pe listă și"),
                Segment("silence", 600),
                Segment("speech", 850, "niște lapte"),
                Segment("silence", 2_000),
            ],
        ),
        FixtureSpec(
            name="ro_long_thought",
            language=Language.RO,
            description="Două propoziții lungi cu pauză de 480 ms.",
            segments=[
                Segment("silence", 300),
                Segment("speech", 1_700, "vreau să te uiți în calendar săptămâna viitoare"),
                Segment("silence", 480),
                Segment("speech", 1_300, "și să îmi spui ce dimineți sunt libere"),
                Segment("silence", 2_000),
            ],
        ),
        FixtureSpec(
            name="ro_two_word",
            language=Language.RO,
            description="Enunț foarte scurt.",
            segments=[
                Segment("silence", 300),
                Segment("speech", 540, "oprește tot"),
                Segment("silence", 2_000),
            ],
        ),
    ]


def wakeword_negative_specs(count: int = 12) -> list[FixtureSpec]:
    """Negative audio for a false-accept count: speech-shaped, but never the keyword.

    SYNTHETIC. A real FA/hour figure needs hours of the user's own household audio, and no
    number produced from this set may be presented as a privacy claim.
    """
    specs: list[FixtureSpec] = []
    for index in range(count):
        language = Language.RO if index % 2 else Language.EN
        specs.append(
            FixtureSpec(
                name=f"negative_{index:02d}",
                language=language,
                description="Synthetic negative: continuous speech-shaped audio, no keyword.",
                noise_floor_dbfs=-58.0 + (index % 4) * 3.0,
                speech_dbfs=-26.0 + (index % 3) * 2.0,
                segments=[
                    Segment("silence", 400),
                    Segment("speech", 4_000),
                    Segment("silence", 600),
                    Segment("speech", 3_000),
                    Segment("silence", 1_000),
                ],
            )
        )
    return specs


def wakeword_positive_specs(count: int = 10) -> list[FixtureSpec]:
    """Positive audio for a false-reject count: one keyword marker, then a command.

    The marker is followed immediately by speech with no gap, which is the case that breaks
    a wakeword pipeline with no pre-roll: detection lands after the keyword has finished, so
    the first word of the command is already gone unless the ring buffer holds it.
    """
    specs: list[FixtureSpec] = []
    for index in range(count):
        language = Language.RO if index % 2 else Language.EN
        specs.append(
            FixtureSpec(
                name=f"positive_{index:02d}",
                language=language,
                description="Synthetic positive: keyword marker immediately followed by speech.",
                noise_floor_dbfs=-60.0 + (index % 3) * 4.0,
                segments=[
                    Segment("silence", 300 + 100 * (index % 5)),
                    Segment("keyword", 480),
                    Segment("speech", 1_400, "turn the lights off"),
                    Segment("silence", 1_200),
                ],
            )
        )
    return specs


def keyword_window_ms(spec: FixtureSpec) -> tuple[int, int]:
    """When a detection counts as detecting *this* keyword.

    Opens at the marker start and closes 800 ms after it ends: a wakeword that fires a
    second into the following command has not detected the keyword, it has got lucky.
    """
    cursor = 0
    for segment in spec.segments:
        if segment.kind == "keyword":
            return cursor, cursor + segment.duration_ms + 800
        cursor += segment.duration_ms
    return 0, spec.total_ms


@dataclass
class Manifest:
    kind: str
    synthetic: bool
    generator: str
    fixtures: list[dict] = field(default_factory=list)


def generate(kind: str, root: Path | str = DEFAULT_DIR, *, force: bool = False) -> Path:
    specs = {
        "endpointing": endpointing_specs,
        "wakeword_negative": wakeword_negative_specs,
        "wakeword_positive": wakeword_positive_specs,
    }[kind]()
    directory = Path(root) / kind
    directory.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(kind=kind, synthetic=True, generator="ars_voice.fixtures")
    for index, spec in enumerate(specs):
        path = directory / f"{spec.name}.wav"
        if force or not path.is_file():
            write_wav(path, spec.render(seed=index * 17))
        entry = asdict(spec)
        entry["language"] = spec.language.value
        entry["file"] = path.name
        entry["speech_end_ms"] = spec.speech_end_ms
        entry["total_ms"] = spec.total_ms
        manifest.fixtures.append(entry)
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(asdict(manifest), indent=2, ensure_ascii=False) + "\n")
    return manifest_path


def load_specs(kind: str, root: Path | str = DEFAULT_DIR) -> list[tuple[FixtureSpec, Path]]:
    directory = Path(root) / kind
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"no fixture manifest at {manifest_path} — run `ars-voice-fixtures {kind}` first"
        )
    manifest = json.loads(manifest_path.read_text())
    out: list[tuple[FixtureSpec, Path]] = []
    for entry in manifest["fixtures"]:
        spec = FixtureSpec(
            name=entry["name"],
            language=Language(entry["language"]),
            segments=[Segment(**s) for s in entry["segments"]],
            noise_floor_dbfs=entry.get("noise_floor_dbfs", -62.0),
            speech_dbfs=entry.get("speech_dbfs", -22.0),
            description=entry.get("description", ""),
            synthetic=entry.get("synthetic", True),
        )
        out.append((spec, directory / entry["file"]))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate A.R.S voice fixtures (synthetic).")
    parser.add_argument(
        "kind", choices=["endpointing", "wakeword_negative", "wakeword_positive", "all"]
    )
    parser.add_argument("--root", default=str(DEFAULT_DIR))
    parser.add_argument("--force", action="store_true", help="re-render existing wavs")
    args = parser.parse_args(argv)
    kinds = (
        ["endpointing", "wakeword_negative", "wakeword_positive"]
        if args.kind == "all"
        else [args.kind]
    )
    for kind in kinds:
        path = generate(kind, args.root, force=args.force)
        print(f"wrote {path}")
    print("NOTE: these fixtures are synthetic. Timing conclusions only, never accuracy claims.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
