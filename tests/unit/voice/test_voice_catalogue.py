"""The voice catalogue: naming, honesty about Romanian, and the cost of holding voices.

`SynthesisRequest.voice` used to be ignored outright, so every one of these behaviours is
new and none of them has ever had a chance to regress. The two that matter most:

* a voice the user picked and did not get is invisible in a text log and obvious in the
  audio, so resolution is asserted rather than trusted;
* lazy loading is what makes thirty-one voices affordable, and pinning is what stops lazy
  loading from costing a 315 ms model load in the middle of an ordinary turn.
"""

from __future__ import annotations

import importlib.util
import json
import statistics
import time
from pathlib import Path

import numpy as np
import pytest
from ars_protocol import SUPPORTED_LANGUAGES, Language, SynthesisRequest
from ars_voice.metrics import BUDGET_P95_MS, Stage
from ars_voice.tts.catalogue import (
    CHARACTERS,
    ROMANIAN_LIMITATION,
    VOICES,
    VOICES_BY_NAME,
    VoiceGender,
    caveats_for,
    describe,
    describe_character,
    missing_translations,
    parse_voice_spec,
    voices,
)
from ars_voice.tts.piper import PiperTtsEngine, UnknownVoiceError

ROOT = Path(__file__).resolve().parents[3]
MODELS = ROOT / "models" / "tts"

needs_models = pytest.mark.skipif(
    not (MODELS / "en_GB-vctk-medium.onnx").is_file(), reason="piper voices missing"
)
needs_piper = pytest.mark.skipif(
    importlib.util.find_spec("piper") is None or not (MODELS / "en_US-amy-medium.onnx").is_file(),
    reason="piper package or voices missing",
)


class FakeLoadEngine(PiperTtsEngine):
    """The engine with the 315 ms of ONNX replaced by a string.

    Lazy loading, eviction and pinning are policy, and policy has to be right on a machine
    with no weights — which is every CI machine there will ever be.
    """

    async def _load_model(self, name: str) -> str:
        return f"model:{name}"

    def _prime(self, voice: object, language: Language, selection: object = None) -> None:
        """Priming is a real synthesis and there is no real model here. Warm-up timings are
        meaningless in this subclass; which voices it *loads* is the point."""


# --------------------------------------------------------------------------- the roster

def test_every_user_facing_string_exists_in_english_romanian_and_german():
    """CLAUDE.md #5. A voice picker whose labels fall back to English for a Romanian user
    is the exact failure that rule exists to prevent, and it is the kind that ships because
    the developer reads English."""
    assert missing_translations() == []
    for profile in VOICES:
        for language in SUPPORTED_LANGUAGES:
            assert describe(profile, language).strip()
    for name in CHARACTERS:
        for language in SUPPORTED_LANGUAGES:
            assert describe_character(name, language).strip()


def test_every_language_has_voices_and_english_and_german_have_both_genders():
    """'More voices' has to mean more voices *in the languages A.R.S speaks*, not more
    English ones. Romanian is the exception and it is asserted separately, out loud."""
    for language in SUPPORTED_LANGUAGES:
        assert voices(language=language), f"no voices at all for {language.value}"
    for language in (Language.EN, Language.DE):
        assert voices(language=language, gender=VoiceGender.FEMALE)
        assert voices(language=language, gender=VoiceGender.MALE)


def test_romanian_says_out_loud_that_its_only_voice_is_male():
    """Piper has exactly one Romanian voice in existence and it is male. The failure mode
    to prevent is not the missing voice — it is pretending otherwise: shipping an English
    voice under a Romanian name, or labelling a pitch-shifted man as a woman. Every
    Romanian entry therefore carries the caveat, in all three languages."""
    romanian = voices(language=Language.RO)
    assert romanian, "Romanian must not be silently absent from the catalogue"
    assert not voices(language=Language.RO, gender=VoiceGender.FEMALE)
    assert {v.model for v in romanian} == {"ro_RO-mihai-medium"}
    for profile in romanian:
        assert "ro_only_male" in profile.caveats
        for language in SUPPORTED_LANGUAGES:
            assert caveats_for(profile, language)[0] == ROMANIAN_LIMITATION[language]
    derived = [v for v in romanian if v.is_derived]
    assert derived, "the free pitch/formant shift is the only extra Romanian timbre there is"
    assert all(v.gender is not VoiceGender.FEMALE for v in derived)


def test_no_high_quality_voice_is_offered_on_the_turn_path():
    """`high` Piper voices measure 395-436 ms to first audio (262 ms even on a short first
    sentence) against a 120 ms budget. They are the nicest-sounding rows in the catalogue,
    which is exactly why the exclusion needs a test and not a note."""
    assert not [v for v in VOICES if v.model.endswith("-high")]


def test_multi_speaker_models_are_what_make_the_roster_affordable():
    """One 77 MB file carrying twelve named voices is the entire economic argument for this
    design. If a refactor ever gives every voice its own model, this fails and it should."""
    from_multi_speaker = [v for v in VOICES if v.speaker is not None]
    files = {v.model for v in from_multi_speaker}
    assert len(files) <= 5
    assert len(from_multi_speaker) >= 15, "the roster stopped exploiting multi-speaker models"
    assert len(VOICES) > len({v.model for v in VOICES}), "more voices than downloads"


def test_speaker_ids_are_recorded_where_and_only_where_the_model_has_speakers():
    for profile in VOICES:
        assert (profile.speaker is None) == (profile.speaker_id is None)


# --------------------------------------------------------------------------- spec parsing

def test_a_caller_picks_a_voice_by_name_not_by_speaker_id():
    """The point of the catalogue: nobody should have to know that the British woman they
    like is speaker 72 of `en_GB-vctk-medium`."""
    engine = FakeLoadEngine(model_dir=MODELS)
    selection = engine.resolve("iris", Language.EN)
    assert selection.model == "en_GB-vctk-medium"
    assert selection.speaker_id == 72
    assert selection.formant_k == 1.0


def test_a_catalogue_name_is_a_valid_configured_default():
    """`ARS_TTS_VOICE_EN=poppy` has to work: it is the obvious way to change the assistant's
    voice, and it goes through `PiperTtsEngine(voice_en=...)`, not through a request. The
    trap is residency - what gets pinned and cached is the *model*, and the name and the
    model are no longer the same string."""
    engine = FakeLoadEngine(model_dir=MODELS, voice_en="poppy", voice_ro="mihai_deep")
    assert engine.resolve(None, Language.EN).speaker_id == 3
    assert engine.resolve(None, Language.RO).formant_k == 0.85
    assert engine.pinned_voices == {
        "en_GB-semaine-medium", "ro_RO-mihai-medium", "de_DE-thorsten-medium"
    }


async def test_a_catalogue_default_is_warm_and_pinned_after_warm_up():
    """The 120 ms budget on turn one depends on warm-up having loaded the voice that will
    actually speak. Configuring by catalogue name used to load a file called `poppy.onnx`,
    which does not exist."""
    engine = FakeLoadEngine(model_dir=MODELS, voice_en="poppy")
    await engine.warm_up()
    assert "en_GB-semaine-medium" in engine.resident_voices
    for name in ("en_GB-vctk-medium", "en_US-arctic-medium", "en_GB-alan-medium"):
        await engine._model(name)
    assert "en_GB-semaine-medium" in engine.resident_voices, "the configured default was evicted"


def test_the_configured_default_still_works_as_a_bare_model_name():
    """`ArsConfig.tts_voice_en` is a model name and `voice_for()` hands it straight back
    into `SynthesisRequest.voice`. The catalogue must not break the wiring it arrived into.
    """
    engine = FakeLoadEngine(model_dir=MODELS)
    for language in SUPPORTED_LANGUAGES:
        default = engine.voice_for(language)
        assert engine.resolve(default, language).model == default
        assert engine.resolve(None, language).model == default
        assert engine.resolve("", language).model == default


def test_a_character_can_be_asked_for_without_naming_a_voice():
    """"Talk like a robot" carries no opinion about which voice. That has to resolve to the
    language's default voice, which is what makes characters work in Romanian on day one."""
    assert parse_voice_spec("robot").character == "robot_ring"
    assert parse_voice_spec("robot").voice is None
    assert parse_voice_spec("dalek/alan") == parse_voice_spec("robot_dalek/alan")
    assert parse_voice_spec("alan").character is None
    with pytest.raises(ValueError, match="unknown character"):
        parse_voice_spec("wizard/alan")


@needs_models
def test_the_spec_syntax_reaches_speakers_the_roster_does_not_name():
    """The curated roster is 22 English voices; the model files hold 150. A picker that
    wants all of them must not need a code change to get at them."""
    engine = FakeLoadEngine(model_dir=MODELS)
    everyone = engine.speakers_in("en_GB-vctk-medium")
    assert len(everyone) == 109
    selection = engine.resolve("en_GB-vctk-medium#p225", Language.EN)
    assert selection.speaker_id == everyone["p225"]
    with pytest.raises(UnknownVoiceError, match="no speaker"):
        engine.resolve("en_GB-vctk-medium#nobody", Language.EN)


@needs_models
def test_catalogue_speaker_ids_match_the_model_files_on_disk():
    """The id is recorded in the catalogue so selection costs no file read. That is only
    safe while it agrees with the model's own map — a re-released model that renumbers its
    speakers would otherwise change who is talking, silently, on every multi-speaker voice.
    """
    for profile in VOICES:
        if profile.speaker is None:
            continue
        sidecar = MODELS / f"{profile.model}.onnx.json"
        if not sidecar.is_file():
            pytest.skip(f"{profile.model} not downloaded")
        mapping = json.loads(sidecar.read_text())["speaker_id_map"]
        assert mapping[profile.speaker] == profile.speaker_id, profile.name


def test_an_unknown_voice_falls_back_loudly_instead_of_killing_the_turn():
    """A voice name that does not resolve is a bug somewhere upstream, but the user is
    mid-sentence. Default behaviour is to speak in the default voice and count it; strict
    mode raises, which is what a settings screen should use so a bad pick is visible where
    it is made rather than three turns later."""
    engine = FakeLoadEngine(model_dir=MODELS)
    selection = engine._select(SynthesisRequest(text="hi", language=Language.EN, voice="nope"))
    assert selection.model == engine.voice_for(Language.EN)
    assert engine.voice_fallbacks == 1

    strict = FakeLoadEngine(model_dir=MODELS, strict_voices=True)
    with pytest.raises(UnknownVoiceError, match="unknown voice"):
        strict.resolve("nope", Language.EN)


def test_a_german_voice_is_not_used_to_read_an_english_sentence():
    """Piper phonemises with the voice's own language rules, so `kerstin` reading English
    is not a German accent, it is wrong words. Mismatch is a fallback, never a substitution
    nobody notices."""
    engine = FakeLoadEngine(model_dir=MODELS)
    with pytest.raises(UnknownVoiceError, match="speaks de"):
        engine.resolve("kerstin", Language.EN)
    selection = engine._select(SynthesisRequest(text="hi", language=Language.EN, voice="kerstin"))
    assert selection.model == engine.voice_for(Language.EN)


# --------------------------------------------------------------------------- residency

async def test_voices_load_on_first_use_not_at_startup():
    """Thirty-one voices at 95-110 MB and ~315 ms each is 3 GB and ten seconds of startup.
    Nothing may be loaded until someone actually asks for it."""
    engine = FakeLoadEngine(model_dir=MODELS)
    assert engine.resident_voices == ()
    await engine._model("en_GB-vctk-medium")
    assert engine.resident_voices == ("en_GB-vctk-medium",)
    assert engine.voice_loads == 1


async def test_twelve_voices_in_one_model_file_cost_exactly_one_load():
    """This is the whole reason multi-speaker models were chosen over twelve separate ones:
    the cache is keyed by model, so picking a different VCTK speaker is free."""
    engine = FakeLoadEngine(model_dir=MODELS)
    for name in ("iris", "wren", "esme", "ivor", "gareth", "rufus"):
        selection = engine.resolve(name, Language.EN)
        await engine._model(selection.model)
    assert engine.voice_loads == 1
    assert engine.resident_voices == ("en_GB-vctk-medium",)


async def test_the_resident_set_is_bounded_and_evicts_least_recently_used():
    """Unbounded caching of a 100 MB object is how a local assistant becomes the reason the
    machine swaps."""
    engine = FakeLoadEngine(model_dir=MODELS, max_resident_voices=4)
    await engine.warm_up()
    for name in ("en_GB-semaine-medium", "en_US-arctic-medium", "en_GB-alan-medium"):
        await engine._model(name)
        assert len(engine.resident_voices) <= 4
    assert engine.voice_evictions == 2
    assert engine.resident_voices[-1] == "en_GB-alan-medium"
    assert "en_GB-semaine-medium" not in engine.resident_voices, "oldest should have gone first"


async def test_the_default_voices_are_pinned_and_survive_any_amount_of_voice_hopping():
    """The failure this prevents: the user tries five character voices, the LRU evicts the
    English default, and their next ordinary sentence pays a 315 ms model load against a
    120 ms budget — on a turn where they did nothing unusual at all."""
    engine = FakeLoadEngine(model_dir=MODELS, max_resident_voices=4)
    await engine.warm_up()
    pinned = engine.pinned_voices
    for name in ("en_GB-vctk-medium", "en_GB-semaine-medium", "en_US-arctic-medium",
                 "en_GB-alan-medium", "en_US-sam-medium"):
        await engine._model(name)
    assert pinned <= set(engine.resident_voices)
    assert engine.voice_loads == len(pinned) + 5, "a pinned voice was evicted and reloaded"


async def test_warm_up_loads_the_active_voice_per_language_and_not_the_catalogue():
    """`warm_up` used to mean "load every voice this engine knows about". With a catalogue
    that is 3 GB. It must stay proportional to the number of languages, not the roster."""
    engine = FakeLoadEngine(model_dir=MODELS)
    timings = await engine.warm_up()
    assert set(timings) == set(SUPPORTED_LANGUAGES)
    assert engine.voice_loads == len(SUPPORTED_LANGUAGES) < len(VOICES)
    assert set(engine.resident_voices) == engine.pinned_voices


# --------------------------------------------------------------------------- real weights

def _median_f0(pcm: bytes, sr: int = 16000) -> float:
    """Autocorrelation pitch track, median over voiced frames. A gender *proxy* and the same
    one the catalogue's numbers were measured with."""
    x = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768.0
    win, hop = int(0.04 * sr), int(0.01 * sr)
    found: list[float] = []
    for offset in range(0, len(x) - win, hop):
        frame = x[offset : offset + win] - x[offset : offset + win].mean()
        if np.sqrt(np.mean(frame**2)) < 0.02:
            continue
        corr = np.correlate(frame, frame, "full")[win - 1 :]
        corr /= corr[0] + 1e-12
        lo, hi = int(sr / 400), int(sr / 60)
        peak = int(np.argmax(corr[lo:hi])) + lo
        if corr[peak] < 0.35:
            continue
        found.append(sr / peak)
    return statistics.median(found) if len(found) >= 8 else float("nan")


async def _render(engine: PiperTtsEngine, voice: str, language: Language, text: str) -> bytes:
    pcm = bytearray()
    async for chunk in engine.synthesize(
        SynthesisRequest(text=text, language=language, voice=voice)
    ):
        pcm += chunk.pcm
    return bytes(pcm)


_LINES = {
    Language.EN: "Good morning, I found three new messages from the bank.",
    Language.RO: "Bună dimineața, am găsit trei mesaje noi de la bancă.",
    Language.DE: "Guten Morgen, ich habe drei neue Nachrichten von der Bank.",
}


@needs_piper
async def test_speaker_id_actually_selects_a_different_speaker():
    """The bug this exists for: `SynthesisConfig.speaker_id` was never set, so every one of
    the 109 VCTK voices rendered as speaker 0 and the catalogue would have been an elaborate
    way of always getting the same person. Measured F0 is the cheapest proof they differ."""
    engine = PiperTtsEngine(model_dir=MODELS)
    pitches = {}
    for name in ("ivor", "iris", "wren"):
        pcm = await _render(engine, name, Language.EN, _LINES[Language.EN])
        pitches[name] = _median_f0(pcm)
        assert abs(pitches[name] - VOICES_BY_NAME[name].median_f0_hz) < 15, (
            f"{name} no longer sounds like the catalogue says: {pitches[name]:.0f} Hz"
        )
    assert pitches["ivor"] < pitches["iris"] < pitches["wren"]
    assert engine.voice_loads == 1, "three voices from one file must cost one load"


@needs_piper
async def test_the_derived_romanian_voice_really_is_shifted_and_is_not_female():
    """The only extra Romanian timbre available, and the free one: lying to the resampler
    about the source rate moves pitch and formants together. It must actually move — and it
    must not be sold as the female voice it cannot be. 157 Hz is a smaller man."""
    engine = PiperTtsEngine(model_dir=MODELS)
    base = _median_f0(await _render(engine, "mihai", Language.RO, _LINES[Language.RO]))
    light = _median_f0(await _render(engine, "mihai_light", Language.RO, _LINES[Language.RO]))
    semitones = 12 * np.log2(light / base)
    expected = 12 * np.log2(VOICES_BY_NAME["mihai_light"].formant_k)
    assert abs(semitones - expected) < 1.0, f"shift {semitones:.2f} st, expected {expected:.2f}"
    assert VOICES_BY_NAME["mihai_light"].gender is not VoiceGender.FEMALE


@needs_piper
async def test_every_model_in_the_catalogue_meets_the_time_to_first_audio_budget():
    """The budget is the constraint the whole catalogue was selected against: `high` voices
    were excluded for missing it by 3x, and a voice that misses it is a voice that must not
    be offered. One representative per model file, warm, median of three."""
    budget = BUDGET_P95_MS[Stage.TTS_FIRST_AUDIO]
    engine = PiperTtsEngine(model_dir=MODELS, first_sentence_max_chars=90)
    seen: set[str] = set()
    slow: list[str] = []
    measured: dict[str, float] = {}
    for profile in VOICES:
        if profile.model in seen:
            continue
        seen.add(profile.model)
        text = _LINES[profile.language]
        request = SynthesisRequest(text=text, language=profile.language, voice=profile.name)
        await _render(engine, profile.name, profile.language, text)  # warm this voice
        samples = []
        for _ in range(5):
            started = time.perf_counter()
            async for chunk in engine.synthesize(request):
                if chunk.pcm:
                    samples.append((time.perf_counter() - started) * 1000)
                    break
        median = statistics.median(samples)
        measured[profile.name] = median
        if median >= budget:
            slow.append(f"{profile.name} ({profile.model}) {median:.0f} ms")
    assert not slow, (
        f"over the {budget:.0f} ms budget: {slow} "
        f"(all: {', '.join(f'{k} {v:.0f}' for k, v in measured.items())})"
    )
