"""Speech recognition. Bilingual EN/RO, detected per utterance."""

from .faster_whisper_engine import FasterWhisperEngine
from .language import (
    LanguageArbiter,
    LanguageDecision,
    constrain_probabilities,
    word_count,
)
from .mlx_whisper_engine import MlxWhisperEngine
from .mock import BilingualScript, MockAsrEngine, ScriptedUtterance

__all__ = [
    "BilingualScript", "FasterWhisperEngine", "LanguageArbiter", "LanguageDecision",
    "MlxWhisperEngine", "MockAsrEngine", "ScriptedUtterance", "constrain_probabilities",
    "word_count",
]
