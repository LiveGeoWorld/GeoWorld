from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Any
'Wall-clock measurement plus deterministic virtual episode time.'

class TimeManager:
    """Keep model-runtime measurement separate from benchmark virtual time."""

    def __init__(self):
        self._start = time.time() * 1000
        self._virtual_time: datetime | None = None

    def now_ms(self) -> int:
        return int(time.time() * 1000 - self._start)

    def set_simulated_time(self, value: Any) -> None:
        """Backward-compatible alias for setting the virtual episode clock."""
        self.set_virtual_time(value)

    def get_simulated_time(self) -> str | None:
        return self.virtual_time_iso()

    def set_virtual_time(self, value: Any) -> datetime:
        parsed = self._parse_time(value)
        self._virtual_time = parsed
        return parsed

    def advance_to(self, value: Any) -> datetime:
        parsed = self._parse_time(value)
        if self._virtual_time is not None and parsed < self._virtual_time:
            raise ValueError('Virtual time cannot move backwards')
        self._virtual_time = parsed
        return parsed

    def virtual_time(self) -> datetime | None:
        return self._virtual_time

    def virtual_time_iso(self) -> str | None:
        return self._virtual_time.isoformat() if self._virtual_time else None

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)):
            parsed = datetime.fromtimestamp(value, tz=timezone.utc)
        elif isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        else:
            raise ValueError(f'Invalid virtual time: {value!r}')
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
