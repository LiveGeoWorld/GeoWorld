"""Ambient, redacted external-usage reporting.

Task construction code can bind a recorder for the current batch. Shared
clients remain usable outside task construction: without a recorder this is a
no-op. Request payloads, API keys, and provider response bodies are never
recorded here.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Protocol


class ExternalUsageRecorder(Protocol):
    def record_external_usage(self, **event: Any) -> None: ...


_RECORDER: ContextVar[ExternalUsageRecorder | None] = ContextVar(
    "geoword_external_usage_recorder", default=None,
)


@contextmanager
def bind_external_usage_recorder(recorder: ExternalUsageRecorder) -> Iterator[None]:
    token = _RECORDER.set(recorder)
    try:
        yield
    finally:
        _RECORDER.reset(token)


def record_external_usage(**event: Any) -> None:
    recorder = _RECORDER.get()
    if recorder is not None:
        recorder.record_external_usage(**event)
