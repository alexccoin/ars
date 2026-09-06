"""A.R.S telemetry — spans and metrics, stdlib only.

NOTE ON SCOPE: this package was an empty stub in the workspace before this change. A
backend engineer building `services/memory` needed *something* real to instrument
against (CLAUDE.md: "a handler with no instrumentation is not finished"), so this is a
deliberately small, dependency-free implementation: structured log records for spans,
in-process counters/histograms with a `.snapshot()` for tests. It has no exporter, no
OTLP, no vendor SDK — swapping in a real backend (OpenTelemetry SDK, etc.) later is an
implementation detail behind this same module surface, consistent with the "no vendor
SDK outside a backend implementation" rule. Whoever owns this package long-term should
review the API, not just the internals.

Privacy note: spans and metrics carry *attributes*, never raw user content. Callers in
this codebase pass identifiers (record ids, session ids, counts) — never transcript or
memory text. This module does not enforce that (it cannot know what a string means) —
callers are responsible, per the privacy rules in CLAUDE.md.
"""

from __future__ import annotations

from .metrics import Counter, Histogram, counter, histogram
from .tracing import aspan, span

__all__ = ["Counter", "Histogram", "aspan", "counter", "histogram", "span"]
