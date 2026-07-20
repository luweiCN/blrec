from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Optional

__all__ = ('RequestMetrics', 'record_database_call', 'request_metrics_scope')


@dataclass
class RequestMetrics:
    database_calls: int = 0
    database_ms: float = 0.0


_current: ContextVar[Optional[RequestMetrics]] = ContextVar(
    'blrec_request_metrics', default=None
)


@contextmanager
def request_metrics_scope() -> Iterator[RequestMetrics]:
    metrics = RequestMetrics()
    token = _current.set(metrics)
    try:
        yield metrics
    finally:
        _current.reset(token)


def record_database_call(elapsed_seconds: float) -> None:
    metrics = _current.get()
    if metrics is None:
        return
    metrics.database_calls += 1
    metrics.database_ms += max(0.0, elapsed_seconds * 1000.0)
