from __future__ import annotations
from typing import Any
'Event queue — reserved for dynamic environment event injection.\n\nCurrent state: placeholder. Future use:\n  - Weather changes (rain, heat)\n  - Transit delays\n  - Service closures\n  - Actor preference changes\n'

class EventQueue:
    """Queues environment events for injection during agent execution."""

    def __init__(self):
        self._queue: list[dict[str, Any]] = []

    def push(self, event_type: str, payload: dict[str, Any]) -> None:
        self._queue.append({'type': event_type, 'payload': payload})

    def pop_pending(self) -> list[dict[str, Any]]:
        pending = list(self._queue)
        self._queue.clear()
        return pending

    def __len__(self) -> int:
        return len(self._queue)
