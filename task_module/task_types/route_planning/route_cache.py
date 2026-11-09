"""RouteCache — persistent route cache with Google Maps API integration.

Keyed by origin/dest/mode, deduplicates across tasks.
Stored at outputs/routes/{city_id}/route_cache.json.

Self-healing: old empty records (distance_m==0) are dropped on load;
retried up to 3 times on API failure; unavailable modes marked permanently.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from shared.route_coordinate_keys import (
    _normalized_coordinate as _normalized_coordinate,
    build_coordinate_key as _build_coordinate_key,
    coordinate_key_from_entry as _coordinate_key_from_entry,
)

from shared.io import read_json, write_json
from shared.external_usage import record_external_usage


_JOURNAL_LOCKS: dict[str, threading.RLock] = {}
_JOURNAL_LOCKS_GUARD = threading.Lock()


def _journal_lock(path: Path) -> threading.RLock:
    key = str(path.resolve()).casefold()
    with _JOURNAL_LOCKS_GUARD:
        return _JOURNAL_LOCKS.setdefault(key, threading.RLock())


class RouteCachePersistenceError(RuntimeError):
    """A provider result could not be durably recorded locally."""


class RouteCache:
    """Cross-task route cache. Deduplicates by (origin, dest, mode, waypoint_lat/lng)."""

    def __init__(self, city_id: str, output_root: str | None = None,
                 min_request_interval_s: float = 0.125, cache_namespace: str | None = None):
        self.city_id = city_id
        # A managed-world batch sets an explicit, versioned environment
        # context. Existing standalone invocations keep the legacy location.
        resolved_root = output_root or os.environ.get("GEOWORD_ROUTE_CACHE_ROOT", "./outputs/routes")
        resolved_namespace = cache_namespace
        if resolved_namespace is None:
            resolved_namespace = os.environ.get("GEOWORD_ROUTE_CACHE_NAMESPACE", "")
        self.output_root = Path(resolved_root)
        self.cache_namespace = str(resolved_namespace).strip()
        self.cache_dir = self.output_root / city_id
        if self.cache_namespace:
            self.cache_dir /= self.cache_namespace
        self.cache_path = self.cache_dir / "route_cache.json"
        self.pending_path = self.cache_dir / "route_cache.pending.jsonl"
        self.cost_events_path = self.cache_dir / "route_cost_events.jsonl"
        self.cost_run_id = str(os.environ.get("GEOWORD_ROUTE_COST_RUN_ID") or "unattributed")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._cache: dict[str, dict[str, Any]] = self._load()
        self._route_id_index: dict[str, dict[str, Any]] = {}
        self._coordinate_index: dict[str, dict[str, Any]] = {}
        self._rebuild_indexes()
        self.min_request_interval_s = max(0.0, min_request_interval_s)
        self._last_request_at = 0.0
        self.stats: dict[str, float | int] = {
            "cache_hits": 0, "cache_misses": 0, "api_requests": 0,
            "api_retries": 0, "api_failures": 0, "unavailable_routes": 0,
            "coordinate_reuse_materialized": 0,
            "rate_limit_wait_s": 0.0, "api_elapsed_s": 0.0,
        }

    def get(
        self, origin_place: dict, dest_place: dict, mode: str,
        waypoint_place: dict | None = None, snapshot_id: str = "static_v1",
    ) -> dict[str, Any] | None:
        """Get cached route, or None if not found."""
        key = self._make_key(origin_place, dest_place, mode, waypoint_place, snapshot_id)
        exact = self._cache.get(key)
        if exact is not None:
            return self._resolved_entry(exact)

        coordinate_key = _build_coordinate_key(
            origin_place, dest_place, mode, waypoint_place, snapshot_id,
        )
        canonical = self._coordinate_index.get(coordinate_key or "")
        if canonical is None:
            return None

        alias = {
            "route_id": f"route_{self.city_id}_{key[:12]}",
            "route_ref": canonical.get("route_id", ""),
            "reuse_type": "coordinate",
            "origin_place_id": origin_place.get("place_id", ""),
            "origin_name": origin_place.get("name", ""),
            "origin_lat": origin_place.get("location", {}).get("lat", 0),
            "origin_lng": origin_place.get("location", {}).get("lng", 0),
            "dest_place_id": dest_place.get("place_id", ""),
            "dest_name": dest_place.get("name", ""),
            "dest_lat": dest_place.get("location", {}).get("lat", 0),
            "dest_lng": dest_place.get("location", {}).get("lng", 0),
            "mode": mode,
            "snapshot_id": snapshot_id,
            "waypoint_place_id": waypoint_place.get("place_id") if waypoint_place else None,
            "waypoint_name": waypoint_place.get("name", "") if waypoint_place else None,
        }
        self._cache[key] = alias
        self._route_id_index[alias["route_id"]] = alias
        self.stats["coordinate_reuse_materialized"] += 1
        self._append_pending(alias)
        self._append_cost_event("coordinate_reuse")
        return self._resolved_entry(alias)

    def put(
        self, origin_place: dict, dest_place: dict, mode: str,
        route_data: dict[str, Any],
        waypoint_place: dict | None = None, snapshot_id: str = "static_v1",
    ) -> str:
        """Store a route and return its key."""
        key = self._make_key(origin_place, dest_place, mode, waypoint_place, snapshot_id)
        self._cache[key] = {
            "route_id": f"route_{self.city_id}_{key[:12]}",
            "origin_place_id": origin_place.get("place_id", ""),
            "origin_name": origin_place.get("name", ""),
            "origin_lat": origin_place.get("location", {}).get("lat", 0),
            "origin_lng": origin_place.get("location", {}).get("lng", 0),
            "dest_place_id": dest_place.get("place_id", ""),
            "dest_name": dest_place.get("name", ""),
            "dest_lat": dest_place.get("location", {}).get("lat", 0),
            "dest_lng": dest_place.get("location", {}).get("lng", 0),
            "mode": mode,
            "snapshot_id": snapshot_id,
            "waypoint_place_id": waypoint_place.get("place_id", "") if waypoint_place else None,
            "waypoint_name": waypoint_place.get("name", "") if waypoint_place else None,
            "route": route_data,
        }
        self._route_id_index[self._cache[key]["route_id"]] = self._cache[key]
        coordinate_key = _build_coordinate_key(
            origin_place, dest_place, mode, waypoint_place, snapshot_id,
        )
        if coordinate_key:
            self._coordinate_index.setdefault(coordinate_key, self._cache[key])
        # Compute walking distance from WALK steps (for constraint generation)
        if "walking_distance_m" not in route_data:
            route_data["walking_distance_m"] = sum(
                s.get("distance_m", 0) for s in route_data.get("steps", [])
                if s.get("travelMode") == "WALK"
            )
        self._append_pending(self._cache[key])
        return key

    def get_or_compute(
        self, origin_place: dict, dest_place: dict, mode: str,
        maps_client: Any,
        waypoint_place: dict | None = None, snapshot_id: str = "static_v1",
    ) -> dict[str, Any]:
        """Get cached route or compute via Google Maps API (retry ≤3 times)."""
        key = self._make_key(origin_place, dest_place, mode, waypoint_place, snapshot_id)

        entry = self.get(origin_place, dest_place, mode, waypoint_place, snapshot_id)

        # Valid cached data → return immediately
        if entry:
            self.stats["cache_hits"] += 1
            record_external_usage(
                provider="route_cache", operation="route_lookup", status="cache_hit",
                cache_status="hit", usage={"request_units": 0},
            )
            return entry

        self.stats["cache_misses"] += 1

        # No cache or old empty record → call API with retries
        origin_lat = origin_place.get("location", {}).get("lat", 0)
        origin_lng = origin_place.get("location", {}).get("lng", 0)
        dest_lat = dest_place.get("location", {}).get("lat", 0)
        dest_lng = dest_place.get("location", {}).get("lng", 0)

        for attempt in range(3):
            try:
                elapsed = time.monotonic() - self._last_request_at
                wait_s = max(0.0, self.min_request_interval_s - elapsed)
                if wait_s:
                    time.sleep(wait_s)
                    self.stats["rate_limit_wait_s"] += wait_s
                started_at = time.monotonic()
                self.stats["api_requests"] += 1
                self._append_cost_event("api_request")
                route_data = maps_client.compute_route(
                    origin_lat, origin_lng, dest_lat, dest_lng, mode,
                    departure_time=snapshot_id if mode == "TRANSIT" and "T" in snapshot_id else None,
                )
                self.stats["api_elapsed_s"] += time.monotonic() - started_at
                self._last_request_at = time.monotonic()
                if route_data is None:
                    self.stats["unavailable_routes"] += 1
                    # API succeeded but no route exists for this mode
                    self.put(origin_place, dest_place, mode,
                             {"available": False, "distance_m": 0, "duration_s": 0, "summary": "UNAVAILABLE", "steps": []},
                             waypoint_place, snapshot_id)
                    return self._cache[key]
                # Success: store and return
                route_data["available"] = True
                self.put(origin_place, dest_place, mode, route_data, waypoint_place, snapshot_id)
                return self._cache[key]
            except RouteCachePersistenceError:
                raise
            except Exception as exc:
                retry = attempt < 2
                if retry:
                    self.stats["api_retries"] += 1
                    time.sleep(2 ** attempt)
                else:
                    self.stats["api_failures"] += 1
                    print(f"  [RouteCache] API failed ×3 {origin_place.get('name','?')}→"
                          f"{dest_place.get('name','?')} ({mode}): {exc}")

        return {"error": "API_FAILED", "api_error": True,
                "distance_m": 0, "duration_s": 0, "summary": "API_ERROR", "steps": []}

    def save(self) -> None:
        with _journal_lock(self.pending_path):
            write_json(self.cache_path, {
                "city_id": self.city_id,
                "cached_pair_count": len(self._cache),
                "generation_statistics": self.stats_snapshot(),
                "cache": list(self._cache.values()),
            })
            if self.pending_path.exists():
                self.pending_path.unlink()

    def stats_snapshot(self) -> dict[str, float | int | None]:
        """Return release-reportable cache, quota, retry, and latency statistics."""
        snapshot: dict[str, float | int | None] = dict(self.stats)
        hits = int(snapshot["cache_hits"] or 0)
        misses = int(snapshot["cache_misses"] or 0)
        snapshot["requests_total"] = hits + misses
        snapshot["cache_hit_rate"] = round(hits / max(1, hits + misses), 4)
        snapshot["rate_limit_qps"] = (
            round(1 / self.min_request_interval_s, 2)
            if self.min_request_interval_s else None
        )
        return snapshot

    def _load(self) -> dict[str, dict[str, Any]]:
        cache: dict[str, dict[str, Any]] = {}
        skipped_empty = 0
        try:
            if self.cache_path.exists():
                data = read_json(self.cache_path)
                for entry in data.get("cache", []):
                    skipped_empty += int(not self._merge_loaded_entry(cache, entry))
            if self.pending_path.exists():
                with _journal_lock(self.pending_path):
                    with self.pending_path.open("r", encoding="utf-8") as handle:
                        for line_number, line in enumerate(handle, start=1):
                            if not line.strip():
                                continue
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
                                print(f"  [RouteCache] Ignored malformed recovery line {line_number}")
                                continue
                            skipped_empty += int(not self._merge_loaded_entry(cache, entry))
            if skipped_empty:
                print(f"  [RouteCache] Skipped {skipped_empty} old empty entries (will retry on demand)")
            return cache
        except Exception:
            return cache

    @staticmethod
    def _merge_loaded_entry(cache: dict[str, dict[str, Any]], entry: Any) -> bool:
        if not isinstance(entry, dict):
            return False
        route = None if entry.get("route_ref") else entry.get("route", {})
        if route is not None and route.get("distance_m", 0) == 0 and "available" not in route:
            return False
        origin = entry.get("origin_place_id", "")
        destination = entry.get("dest_place_id", "")
        mode = entry.get("mode", "")
        waypoint = entry.get("waypoint_place_id")
        snapshot_id = entry.get("snapshot_id") or "static_v1"
        key = _build_key_str(origin, destination, mode, waypoint, snapshot_id)
        if route is not None and route.get("available") is not False and "walking_distance_m" not in route:
            route["walking_distance_m"] = sum(
                step.get("distance_m", 0) for step in route.get("steps", [])
                if step.get("travelMode") == "WALK"
            )
        cache[key] = entry
        return True

    def _append_pending(self, entry: dict[str, Any]) -> None:
        serialized = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with _journal_lock(self.pending_path):
                with self.pending_path.open("a", encoding="utf-8", newline="") as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as exc:
            raise RouteCachePersistenceError(
                f"Could not persist route recovery journal: {self.pending_path}"
            ) from exc

    def _append_cost_event(self, event_type: str) -> None:
        event = {
            "event_type": event_type,
            "run_id": self.cost_run_id,
            "recorded_at_unix": round(time.time(), 3),
        }
        serialized = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with _journal_lock(self.cost_events_path):
                with self.cost_events_path.open("a", encoding="utf-8", newline="") as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as exc:
            raise RouteCachePersistenceError(
                f"Could not persist route cost event: {self.cost_events_path}"
            ) from exc

    def _rebuild_indexes(self) -> None:
        self._route_id_index = {
            str(entry.get("route_id") or ""): entry
            for entry in self._cache.values()
            if entry.get("route_id")
        }
        self._coordinate_index = {}
        for entry in self._cache.values():
            if not entry.get("route") or entry.get("route_ref"):
                continue
            coordinate_key = _coordinate_key_from_entry(entry)
            if coordinate_key:
                self._coordinate_index.setdefault(coordinate_key, entry)

    def _resolved_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        route_ref = str(entry.get("route_ref") or "")
        if not route_ref:
            return entry
        canonical = self._route_id_index.get(route_ref)
        if not canonical or not isinstance(canonical.get("route"), dict):
            return entry
        resolved = dict(entry)
        resolved["route"] = canonical["route"]
        return resolved

    def _make_key(
        self, origin_place: dict, dest_place: dict, mode: str,
        waypoint_place: dict | None = None, snapshot_id: str = "static_v1",
    ) -> str:
        return _build_key_str(
            origin_place.get("place_id", ""),
            dest_place.get("place_id", ""),
            mode,
            waypoint_place.get("place_id") if waypoint_place else None,
            snapshot_id,
        )

    def __len__(self) -> int:
        return len(self._cache)


def _build_key_str(origin_id: str, dest_id: str, mode: str, waypoint_id: str | None,
                   snapshot_id: str = "static_v1") -> str:
    raw = f"{origin_id}|{dest_id}|{mode}|snapshot={snapshot_id}"
    if waypoint_id:
        raw += f"|{waypoint_id}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]
