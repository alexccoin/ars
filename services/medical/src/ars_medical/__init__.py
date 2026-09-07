"""A.R.S personal medical store — vital readings, reference ranges, findings.

`SqliteMedicalStore` persists `VitalReading` (packages/protocol/src/ars_protocol/
health.py), immutable and superseded rather than edited, in its own local SQLite
database. See its module docstring for the ingest/dedup/deletion rules, and
`ars_protocol.health`'s module docstring for why nothing here can express a diagnosis.

Everything in this package defaults to `Sensitivity.SENSITIVE` per the protocol type —
this package does not (and must not) loosen that.
"""

from __future__ import annotations

from .config import MedicalConfig
from .ranges import DEFAULT_REFERENCE_RANGES
from .store import Aggregate, ImplausibleReadingError, IngestResult, SqliteMedicalStore, Trend

__all__ = [
    "DEFAULT_REFERENCE_RANGES",
    "Aggregate",
    "ImplausibleReadingError",
    "IngestResult",
    "MedicalConfig",
    "SqliteMedicalStore",
    "Trend",
]
