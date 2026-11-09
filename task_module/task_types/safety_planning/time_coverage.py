"""City-wide opening coverage before any GT is drawn."""

from collections import Counter
from datetime import datetime, time, timedelta, timezone

from .opening_hours import intervals_on_date, open_at


class OpeningCoverage:
    def __init__(self, world):
        self.places = {p["place_id"]: p for p in world["places"]}
        self.services = {pid: set() for pid in self.places}
        for row in [*world["places"], *world["services"]]:
            if row.get("place_id") not in self.services:
                continue
            for field in ("service_types", "primary_service_type", "service_type"):
                value = row.get(field, [])
                self.services[row["place_id"]].update(v for v in (value if isinstance(value, list) else [value]) if v)
        self._by_day = {}

    def at(self, task_time):
        opened = {pid for pid, p in self.places.items() if open_at(p, task_time) is True}
        counts = Counter(service for pid in opened for service in self.services[pid])
        return {"place_ids": sorted(opened),
                "service_types": sorted(service for service, count in counts.items() if count >= 2)}

    def candidates(self, day, zone):
        key = (day, str(zone))
        if key not in self._by_day:
            boundaries = {0}
            for place in self.places.values():
                for start, end in intervals_on_date(place, day) or []:
                    boundaries.update((start, end))
            rows = []
            for minute in sorted(boundaries - {1440}):
                point = datetime.combine(day, time.min, zone) + timedelta(minutes=minute)
                if point.astimezone(timezone.utc).astimezone(zone) != point:
                    continue
                stamp = point.isoformat()
                coverage = self.at(stamp)
                if coverage["service_types"]:
                    rows.append({"task_time": stamp, **coverage})
            self._by_day[key] = rows
        return list(self._by_day[key])

    def prefer(self, candidates, entries):
        services, places = Counter(), Counter()
        # Repeated tasks in one cluster do not count as new temporal coverage.
        for stamp in sorted({e["context"]["task_time"] for e in entries if "task_time" in e["context"]}):
            previous = self.at(stamp)
            services.update(previous["service_types"])
            places.update(previous["place_ids"])
        if not candidates:
            return []
        lowest = min(services[s] for row in candidates for s in row["service_types"])
        shortlist = [row for row in candidates if any(services[s] == lowest for s in row["service_types"])]
        lowest_poi = min(places[p] for row in shortlist for p in row["place_ids"])
        return [row for row in shortlist if any(places[p] == lowest_poi for p in row["place_ids"])]
