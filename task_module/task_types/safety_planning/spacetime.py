"""Time windows and legacy spatial-atom reservation compatibility.

New Safety choice construction uses evidence_index.SafetyEvidenceIndex, not
these legacy footprint classes. They remain readable for historical snapshots
and must not be populated with route IDs to manufacture spatial separation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from typing import Any


def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Safety timestamps require an explicit timezone")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class TimeWindow:
    start: str
    end: str

    def __post_init__(self) -> None:
        if _instant(self.start) >= _instant(self.end):
            raise ValueError("Safety window must have positive duration")

    def overlaps(self, other: TimeWindow) -> bool:
        return max(_instant(self.start), _instant(other.start)) < min(
            _instant(self.end), _instant(other.end)
        )


@dataclass(frozen=True)
class ResolvedFootprint:
    world_binding: str
    partition: str
    atoms: frozenset[str]
    source_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.world_binding or not self.partition:
            raise ValueError("Spatial footprint requires a world and partition binding")
        if not self.atoms or any(not atom for atom in self.atoms):
            raise ValueError("Unknown spatial coverage cannot be treated as empty coverage")
        if not self.source_refs or any(not ref for ref in self.source_refs):
            raise ValueError("Spatial footprint requires frozen geographic source references")

    def intersects(self, other: ResolvedFootprint) -> bool:
        if self.world_binding != other.world_binding or self.partition != other.partition:
            raise ValueError("Spatial footprints must be resolved against the same frozen partition")
        return not self.atoms.isdisjoint(other.atoms)


@dataclass(frozen=True)
class Reservation:
    owner: str
    role: str
    window: TimeWindow
    footprint: ResolvedFootprint

    def __post_init__(self) -> None:
        if not self.owner or self.role not in {"event", "decision_dependency"}:
            raise ValueError("Reservation requires an owner and an explicit dependency role")


def overlaps(first: Reservation, second: Reservation) -> bool:
    # Validate the geographic binding even when the intervals are disjoint.
    intersects = first.footprint.intersects(second.footprint)
    return intersects and first.window.overlaps(second.window)


class ReservationIndex:
    """Stage a whole candidate atomically; never re-solve prior tasks.

Checks both directions: new events against old dependencies, and new
dependencies against old events. Dependency/dependency overlap is allowed.
The caller supplies every candidate/route dependency, not just the Gold POI.
"""

    def __init__(self, reservations: Iterable[Reservation] = ()) -> None:
        self._reservations = tuple(reservations)

    @property
    def reservations(self) -> tuple[Reservation, ...]:
        return self._reservations

    def conflicts(self, proposed: Iterable[Reservation]) -> list[tuple[Reservation, Reservation]]:
        result = []
        for new in proposed:
            for old in self._reservations:
                if old.owner == new.owner:
                    raise ValueError("An existing task reservation cannot be overwritten")
                if overlaps(new, old) and "event" in {old.role, new.role}:
                    result.append((new, old))
        return result

    def reserve(self, proposed: Iterable[Reservation]) -> None:
        pending = tuple(proposed)
        if not pending or len({item.owner for item in pending}) != 1:
            raise ValueError("Reserve exactly one task at a time")
        if {item.role for item in pending} != {"event", "decision_dependency"}:
            raise ValueError("A task requires both event and decision dependency footprints")
        if self.conflicts(pending):
            raise ValueError("Candidate intersects previously reserved event or decision footprints")
        self._reservations += pending

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": "safety_reservations.v1", "reservations": [
            {"owner": row.owner, "role": row.role,
             "window": {"start": row.window.start, "end": row.window.end},
             "footprint": {"world_binding": row.footprint.world_binding,
                           "partition": row.footprint.partition,
                           "atoms": sorted(row.footprint.atoms),
                           "source_refs": list(row.footprint.source_refs)}}
            for row in self.reservations]}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ReservationIndex:
        if payload.get("schema_version") != "safety_reservations.v1":
            raise ValueError("Unsupported Safety reservation snapshot")
        rows = payload.get("reservations")
        if not isinstance(rows, list):
            raise ValueError("Safety reservation snapshot requires a list")
        grouped: dict[str, list[Reservation]] = {}
        for row in rows:
            footprint = row["footprint"]
            reservation = Reservation(row["owner"], row["role"], TimeWindow(**row["window"]),
                ResolvedFootprint(footprint["world_binding"], footprint["partition"],
                                  frozenset(footprint["atoms"]), tuple(footprint["source_refs"])))
            grouped.setdefault(reservation.owner, []).append(reservation)
        result = cls()
        for pending in grouped.values():
            result.reserve(pending)
        return result
