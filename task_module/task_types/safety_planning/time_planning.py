"""Deterministic, incremental Safety time clusters over a reviewed city calendar."""

import copy
import hashlib
import json
import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from task_module.task_types.safety_planning.event_contract import public_event
from task_module.task_types.safety_planning.spacetime import TimeWindow
from .task_time import applies
from .event_bundle import records


def _cluster_id(window):
    return "cluster_" + hashlib.sha256(json.dumps(window, sort_keys=True).encode()).hexdigest()[:16]


def _validate_window(window, zone):
    result = TimeWindow(**window)
    for value in (result.start, result.end):
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        local = stamp.astimezone(zone)
        if stamp.utcoffset() != local.utcoffset() or local.year != 2026:
            raise ValueError("Safety time must use the city's actual 2026 timezone offset")
        if stamp.replace(tzinfo=None) != local.replace(tzinfo=None):
            raise ValueError("Safety timestamp is not a valid local time")


def visible_event_count(index, window, query_names):
    """Use the public App filters, deduplicating notices, not private protections."""
    from app.apps.safety_event_view import matches_public_scope, matches_time_range
    if not query_names:
        raise ValueError("Time richness requires explicit public geographic queries")
    seen = set()
    for record in records(index.entries):
        event, context = record["event"], record["context"]
        anchor = next(a for a in context["geographic_anchors"] if a["scope_ref"] == event["scope_ref"])
        notice = public_event(event, anchor)
        if (any(matches_public_scope(notice, name) for name in query_names)
                and matches_time_range(notice, window if isinstance(window, str) else f'{window["start"]}/{window["end"]}')):
            seen.add(json.dumps(notice, sort_keys=True, ensure_ascii=False))
    return len(seen)


def local_event_audit(index, context):
    """Count actual public events, never occupancy/protection records or a gate."""
    from shapely.geometry import mapping
    from shapely.ops import unary_union
    from .occupancy import dependencies, geometry, scope_geometry
    current = [geometry(row["geometry"]) for row in dependencies(context)]
    if not current:
        raise ValueError("Event count audit requires candidate coordinates")
    envelope = unary_union(current).envelope
    # Match the point/line envelope handling of pre-event local screening.
    from shapely.geometry import Point, LineString
    x0, y0, x1, y1 = envelope.bounds
    if x0 == x1 and y0 == y1:
        envelope = Point(x0, y0)
    elif x0 == x1 or y0 == y1:
        envelope = LineString([(x0, y0), (x1, y1)])
    seen = set()
    for record in records(index.entries):
        event, source = record["event"], record["context"]
        if not applies(event["active_window"], context["task_time"]):
            continue
        anchor = next(a for a in source["geographic_anchors"] if a["scope_ref"] == event["scope_ref"])
        if envelope.intersects(scope_geometry(anchor, source)):
            seen.add((record["owner"], record.get("event_index", 0)))
    return {"metric": "active_public_events_intersecting_local_envelope.v1", "audit_only": True,
            "task_time": context["task_time"], "local_search_geometry": mapping(envelope), "count": len(seen)}


class SafetyTimeAllocator:
    def __init__(self, *, calendar, policy, task_anchors, world_binding, batch_seed):
        self.calendar = copy.deepcopy(calendar)
        self.policy = copy.deepcopy(policy)
        self.zone = ZoneInfo(calendar["timezone"])
        self.world_binding = world_binding
        self.batch_seed = batch_seed
        self.anchors = copy.deepcopy(task_anchors)
        self.decisions = []

    def choose_point(self, *, slot_id, index, coverage, allowed_targets, selection_seed=None):
        """Select a task instant, with regional eligibility checked before GT draw."""
        if index.world_binding != self.world_binding:
            raise ValueError("Time allocation cannot cross frozen world/city versions")
        seen, skips = set(), []
        history = index.entries

        def slot(row, origin, radius):
            stamp = row["task_time"]
            local = datetime.fromisoformat(stamp).astimezone(self.zone)
            if local.year != 2026:
                return None
            if any(applies(p["active_window"], stamp) for p in self.calendar["periods"] if p["kind"] != "working_day"):
                return None
            key = "cluster_" + hashlib.sha256(stamp.encode()).hexdigest()[:16]
            eligibility = allowed_targets(stamp, key)
            audit = eligibility.get("audit", []) if isinstance(eligibility, dict) else []
            eligible = eligibility["eligible_target_ids"] if isinstance(eligibility, dict) else eligibility
            if not eligible:
                skips.append({"cluster_id": key, "reason": "no_eligible_target_at_time", "target_filter_audit": audit})
                return None
            # Only calendar exclusions bound events; no fixed daily clock range.
            first = datetime(2026, 1, 1, tzinfo=self.zone)
            last = datetime(2027, 1, 1, tzinfo=self.zone)
            for period in self.calendar["periods"]:
                if period["kind"] == "working_day":
                    continue
                start, end = (datetime.fromisoformat(period["active_window"][k]) for k in ("start", "end"))
                if end <= local:
                    first = max(first, end)
                if local < start:
                    last = min(last, start)
            return {"slot_id": slot_id, "cluster_id": key, "task_time": stamp,
                    "event_generation_window": {"start": first.isoformat(), "end": last.isoformat()},
                    "origin": origin, "expansion_radius_days": radius,
                    "eligible_target_ids": sorted(eligible), "opening_coverage": row,
                    "target_filter_audit": audit,
                    "skipped_clusters": list(skips), "query_names": [], "visible_event_count": None,
                    "is_public_holiday": False, "has_known_regional_event": False, "calendar_annotations": []}

        for entry in history:
            stamp = entry["context"].get("task_time")
            if not stamp or stamp in seen:
                continue
            seen.add(stamp)
            row = coverage.at(stamp)
            if row["service_types"]:
                result = slot({"task_time": stamp, **row}, "reuse_completed_cluster", 0)
                if result:
                    return result
        for day, radius in self._dates(self._center(index)):
            if any(p["kind"] != "working_day" and date.fromisoformat(p["start"]) <= day <= date.fromisoformat(p["end"])
                   for p in self.calendar["periods"]):
                continue
            available = []
            for row in coverage.candidates(day, self.zone):
                if row["task_time"] not in seen:
                    candidate = slot(row, "calendar_expansion", radius)
                    if candidate:
                        available.append(candidate)
            if not available:
                continue
            preferred = coverage.prefer([a["opening_coverage"] for a in available], history)
            stamps = {row["task_time"] for row in preferred}
            seed_root = self.batch_seed if selection_seed is None else selection_seed
            seed = hashlib.sha256(f"{seed_root}|safety-time|{slot_id}|{day}".encode()).digest()
            return random.Random(int.from_bytes(seed, "big")).choice([a for a in available if a["task_time"] in stamps])
        raise ValueError("No eligible task time in the reviewed 2026 calendar")

    def _center(self, index):
        for entry in index.entries:
            ctx = entry["context"]
            start = datetime.fromisoformat(ctx["task_time"] if "task_time" in ctx else ctx["activity_window"]["start"]).astimezone(self.zone)
            if start.year != 2026:
                continue
            allocation = entry["context"].get("time_allocation", {})
            if allocation.get("center_date"):
                return date.fromisoformat(allocation["center_date"])
            return start.date()
        if self.policy.get("anchor_date"):
            return date.fromisoformat(self.policy["anchor_date"])
        anchors = []
        for row in self.anchors:
            raw = row.get("time_context", {}).get("instruction_time")
            if not raw:
                continue
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                continue
            local = stamp.astimezone(self.zone)
            if local.year == self.calendar["year"]:
                anchors.append((row.get("task_type") != "safety_planning", local.date()))
        if anchors:
            return min(anchors)[1]
        dates = [date.fromisoformat(r["start"]) for r in self.calendar["periods"] if r["kind"] != "working_day"]
        if not dates:
            raise ValueError("No source-backed 2026 date anchor")
        return min(dates)

    def _dates(self, center):
        before, after = self.policy["preferred_days_before"], self.policy["preferred_days_after"]
        first, last = date(2026, 1, 1), date(2026, 12, 31)
        emitted = set()
        anchors = [(date.fromisoformat(row["start"]), date.fromisoformat(row["end"]))
                   for row in self.calendar.get("core_holidays", [])] or [(center, center)]
        while True:
            for distance, sign in [(n, -1) for n in range(1, before + 1)] + [(n, 1) for n in range(1, after + 1)]:
                for start, end in anchors:
                    # A multi-day holiday is one interval: expand before its
                    # first day or after its last day, never inside it.
                    anchor = start if sign < 0 else end
                    day = anchor + timedelta(days=sign * distance)
                    if day not in emitted and first <= day <= last:
                        emitted.add(day)
                        yield day, distance
            if center - timedelta(days=before) <= first and center + timedelta(days=after) >= last:
                return
            before += self.policy["expansion_step_days"]
            after += self.policy["expansion_step_days"]

    def _windows(self, day):
        def stamp(key):
            return datetime.combine(day, time.fromisoformat(self.policy[key]), self.zone).isoformat()
        return {"activity_window": {"start": stamp("activity_start"), "end": stamp("activity_end")},
                "event_generation_window": {"start": stamp("event_start"), "end": stamp("event_end")}}

    def _blocked_dates(self, slot):
        return [r["source_ref"] for r in self.calendar["periods"] if r["kind"] != "working_day"
                and TimeWindow(**slot["event_generation_window"]).overlaps(TimeWindow(**r["active_window"]))]

    def choose(self, *, slot_id, index, query_names, excluded_clusters=()):
        if index.world_binding != self.world_binding:
            raise ValueError("Time allocation cannot cross frozen world/city versions")
        center = self._center(index)
        candidates = []
        seen = set()
        skips = []
        for entry in index.entries:
            windows = {k: copy.deepcopy(entry["context"][k]) for k in ("activity_window", "event_generation_window")}
            key = _cluster_id(windows)
            if any(datetime.fromisoformat(w["start"]).astimezone(self.zone).year != 2026 or
                   datetime.fromisoformat(w["end"]).astimezone(self.zone).year != 2026 for w in windows.values()):
                skips.append({"cluster_id": key, "reason": "historical_window_outside_2026"})
                continue
            if key not in seen:
                candidates.append((windows, "reuse_completed_cluster", 0))
                seen.add(key)
        excluded_windows = [w["activity_window"] for w, _, _ in candidates if _cluster_id(w) in excluded_clusters]

        def options():
            yield from candidates
            occupied_dates = {datetime.fromisoformat(w["activity_window"]["start"]).astimezone(self.zone).date()
                              for w, _, _ in candidates}
            for day, radius in self._dates(center):
                if day in occupied_dates:
                    continue
                windows = self._windows(day)
                if _cluster_id(windows) not in seen:
                    yield windows, "calendar_expansion", radius

        for windows, origin, radius in options():
            key = _cluster_id(windows)
            for window in windows.values():
                _validate_window(window, self.zone)
            a, e = windows["activity_window"], windows["event_generation_window"]
            if not datetime.fromisoformat(e["start"]) <= datetime.fromisoformat(a["start"]) < datetime.fromisoformat(a["end"]) <= datetime.fromisoformat(e["end"]):
                raise ValueError("Activity must fit its event-generation window")
            if key in excluded_clusters or any(TimeWindow(**a).overlaps(TimeWindow(**w)) for w in excluded_windows):
                skips.append({"cluster_id": key, "reason": "protection_conflict_for_this_slot"})
                continue
            forbidden = self._blocked_dates(windows)
            if forbidden:
                skips.append({"cluster_id": key, "reason": "calendar_avoidance", "source_refs": forbidden})
                continue
            count = visible_event_count(index, a, query_names)
            if count >= self.policy["regional_event_target"]:
                skips.append({"cluster_id": key, "reason": "regional_event_richness", "visible_event_count": count})
                continue
            annotations = [r for r in self.calendar["periods"]
                           if TimeWindow(**e).overlaps(TimeWindow(**r["active_window"]))]
            return {"slot_id": slot_id, "cluster_id": key, **windows, "origin": origin,
                    "center_date": center.isoformat(), "expansion_radius_days": radius,
                    "query_names": list(query_names), "visible_event_count": count,
                    "is_public_holiday": False, "has_known_regional_event": False,
                    "calendar_annotations": annotations, "skipped_clusters": skips}
        raise ValueError("No available 2026 Safety time window under the current calendar and protected decisions")

    def record(self, slot, *, status, reason=None):
        self.decisions.append({**copy.deepcopy(slot), "status": status, "reason": reason})

    def snapshot(self):
        return {"schema_version": "safety_time_plan.v2", "world_binding": self.world_binding,
                "batch_seed": self.batch_seed, "calendar": self.calendar, "policy": self.policy,
                "task_anchors": self.anchors, "allocations": copy.deepcopy(self.decisions)}
