"""Counters and histograms — in-process, thread-safe, with a `.snapshot()` for tests.

Deliberately not a Prometheus/OTLP client: no network, no background thread. A real
exporter can read `.snapshot()` on an interval later without callers changing.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class Counter:
    name: str
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _total: float = 0.0
    _by_attributes: dict[tuple[tuple[str, object], ...], float] = field(default_factory=dict)

    def add(self, value: float = 1.0, **attributes: object) -> None:
        key = tuple(sorted(attributes.items()))
        with self._lock:
            self._total += value
            self._by_attributes[key] = self._by_attributes.get(key, 0.0) + value

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {"total": self._total, "by_attributes": dict(self._by_attributes)}


@dataclass
class Histogram:
    name: str
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _values: list[float] = field(default_factory=list)

    def record(self, value: float, **attributes: object) -> None:
        with self._lock:
            self._values.append(value)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            values = list(self._values)
        if not values:
            return {"count": 0, "sum": 0.0, "min": None, "max": None, "avg": None}
        return {
            "count": len(values),
            "sum": sum(values),
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / len(values),
        }


_counters: dict[str, Counter] = {}
_histograms: dict[str, Histogram] = {}
_registry_lock = threading.Lock()


def counter(name: str) -> Counter:
    """Returns the process-wide counter for `name`, creating it on first use."""
    with _registry_lock:
        if name not in _counters:
            _counters[name] = Counter(name)
        return _counters[name]


def histogram(name: str) -> Histogram:
    """Returns the process-wide histogram for `name`, creating it on first use."""
    with _registry_lock:
        if name not in _histograms:
            _histograms[name] = Histogram(name)
        return _histograms[name]
