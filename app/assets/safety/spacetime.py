from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone

def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('Safety timestamps require an explicit timezone')
    return result.astimezone(timezone.utc)

@dataclass(frozen=True)
class TimeWindow:
    start: str
    end: str

    def __post_init__(self) -> None:
        if _instant(self.start) >= _instant(self.end):
            raise ValueError('Safety window must have positive duration')

    def overlaps(self, other: TimeWindow) -> bool:
        return max(_instant(self.start), _instant(other.start)) < min(_instant(self.end), _instant(other.end))
