"""Spans — structured start/end log records with duration and attributes.

No sampling, no export pipeline: this writes one structured log record per span via the
stdlib `logging` module, on logger `"ars.telemetry.span"`. That is enough to grep or
ship to any log aggregator; a real tracing backend can be layered in later without
touching call sites, because they only ever import `span`/`aspan` from here.
"""

from __future__ import annotations

import contextvars
import logging
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager

_logger = logging.getLogger("ars.telemetry.span")
_current_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ars_current_span_id", default=None
)
_counter = 0


def _next_span_id() -> str:
    global _counter
    _counter += 1
    return f"spn_{_counter:08x}"


@contextmanager
def span(name: str, **attributes: object) -> Iterator[str]:
    """Synchronous span. Usage::

        with span("memory.recall", record_count=3):
            ...

    Yields the span id (useful for correlating a follow-up log line). Exceptions
    propagate unchanged; they are recorded as `ok=False` before re-raising.
    """
    span_id = _next_span_id()
    parent_id = _current_span_id.get()
    token = _current_span_id.set(span_id)
    start = time.perf_counter()
    ok = True
    try:
        yield span_id
    except Exception:
        ok = False
        raise
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        _current_span_id.reset(token)
        _logger.info(
            "span",
            extra={
                "span_id": span_id,
                "parent_id": parent_id,
                "span_name": name,
                "duration_ms": round(duration_ms, 3),
                "ok": ok,
                "attributes": attributes,
            },
        )


@asynccontextmanager
async def aspan(name: str, **attributes: object):
    """Async span — same contract as `span`, for handlers on the await path."""
    span_id = _next_span_id()
    parent_id = _current_span_id.get()
    token = _current_span_id.set(span_id)
    start = time.perf_counter()
    ok = True
    try:
        yield span_id
    except Exception:
        ok = False
        raise
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        _current_span_id.reset(token)
        _logger.info(
            "span",
            extra={
                "span_id": span_id,
                "parent_id": parent_id,
                "span_name": name,
                "duration_ms": round(duration_ms, 3),
                "ok": ok,
                "attributes": attributes,
            },
        )
