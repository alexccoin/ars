"""Deterministic hash-based embedding — no weights, no download, no network.

For tests and CI. It is a hashing-trick bag-of-n-grams: every character trigram and
whole-word token in the (lowercased, diacritic-folded) text is hashed into one of
`EMBEDDING_DIM` buckets with a sign bit, counts are accumulated, and the result is
L2-normalized. This is standard, well-understood, and fully deterministic across
processes (it uses `hashlib`, never Python's randomized `hash()`).

Honest limitation, stated plainly: this backend bridges English and Romanian only
through *shared substrings* — proper nouns, numbers, loanwords, cognates ("Cluj",
"1997", "vegetarian"). It does not know that "I don't eat meat" and "Nu mănânc carne"
mean the same thing; there is no vocabulary overlap between those two sentences, so
their vectors will not be close under this backend. Real cross-lingual *paraphrase*
recall requires `SentenceTransformerEmbeddingBackend`. The diacritic-folding step means
Romanian text typed or transcribed without diacritics still matches text stored with
them, which is realistic (ASR and casual typing frequently drop them).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

import numpy as np

from .base import EMBEDDING_DIM, EmbeddingBackend, normalize

_DIACRITIC_FOLD = str.maketrans({
    "ă": "a", "â": "a", "î": "i", "ș": "s", "ş": "s", "ț": "t", "ţ": "t",
    "Ă": "a", "Â": "a", "Î": "i", "Ș": "s", "Ş": "s", "Ț": "t", "Ţ": "t",
})

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFC", text).translate(_DIACRITIC_FOLD)
    return text.lower()


def _ngrams(word: str, n: int = 3) -> list[str]:
    padded = f"  {word} "
    if len(padded) < n:
        return [padded]
    return [padded[i : i + n] for i in range(len(padded) - n + 1)]


def _bucket_and_sign(token: str, dim: int) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    bucket = value % dim
    sign = 1.0 if (value >> 63) & 1 == 0 else -1.0
    return bucket, sign


class HashEmbeddingBackend(EmbeddingBackend):
    """The default backend (`MemoryConfig.embedding_backend == "hash"`). Safe to run in
    any CI environment, offline, with no model weights on disk."""

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(self._dim, dtype=np.float32)
        folded = _fold(text)
        tokens = _TOKEN_RE.findall(folded)
        for word in tokens:
            bucket, sign = _bucket_and_sign(f"w:{word}", self._dim)
            vector[bucket] += sign
            for gram in _ngrams(word):
                gbucket, gsign = _bucket_and_sign(f"g:{gram}", self._dim)
                vector[gbucket] += 0.5 * gsign
        return normalize(vector)
