"""Per-operation work counters for compile diagnostics.

Provider usage and billing remain with ``MeteredGw``. Work counts and stage
wall times are task-local; response-usage hooks remain compatibility no-ops.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext, contextmanager
from contextvars import ContextVar
from collections import Counter
import time
from typing import Any, Optional


class RuntimeTelemetryRecorder:
    """Per-operation work counts; provider billing remains with MeteredGw."""

    def __init__(self) -> None:
        self.work_counts: Counter[str] = Counter()
        self.stage_wall_ms: Counter[str] = Counter()
        self.stage_calls: Counter[str] = Counter()


_active_recorder: ContextVar[Optional[RuntimeTelemetryRecorder]] = ContextVar(
    "jonex_runtime_recorder", default=None
)


def current_runtime_telemetry() -> Optional[RuntimeTelemetryRecorder]:
    return _active_recorder.get()


def telemetry_stage(
    recorder: Optional[RuntimeTelemetryRecorder], name: str
) -> AbstractContextManager:
    """Accumulate inclusive elapsed time, including failed and nested stages."""
    if recorder is None:
        return nullcontext()

    @contextmanager
    def measured():
        started = time.perf_counter_ns()
        try:
            yield
        finally:
            recorder.stage_wall_ms[name] += (time.perf_counter_ns() - started) / 1_000_000
            recorder.stage_calls[name] += 1

    return measured()


def bind_operation_telemetry(function):
    """Identity decorator — nothing to bind."""
    return function


def bind_runtime_telemetry(recorder):
    @contextmanager
    def bound():
        token = _active_recorder.set(recorder)
        try:
            yield
        finally:
            _active_recorder.reset(token)
    return bound()


def count_work(name: str, amount: int = 1) -> None:
    recorder = current_runtime_telemetry()
    if recorder is not None:
        recorder.work_counts[name] += amount


def record_response_usage(response: Any) -> None:
    """No-op response-usage recorder."""
    del response


def process_peak_rss_bytes() -> Optional[int]:
    """No-op — return ``None``."""
    return None


__all__ = [
    "RuntimeTelemetryRecorder",
    "bind_operation_telemetry",
    "bind_runtime_telemetry",
    "count_work",
    "current_runtime_telemetry",
    "process_peak_rss_bytes",
    "record_response_usage",
    "telemetry_stage",
]
