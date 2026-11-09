"""Persistent, version-bound POI membership for the Routing route matrix."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shared.io import read_json, write_json

from task_module.task_types.route_planning.route_cache import _normalized_coordinate


class RoutePoiPool:
    DEFAULT_MAX_POIS = 50

    def __init__(
        self, route_cache: Any, world: dict[str, Any], snapshot_id: str = "static_v1",
        max_pois: int = DEFAULT_MAX_POIS,
    ) -> None:
        self.path = Path(route_cache.cache_dir) / "route_poi_pool.json"
        self._route_cache = route_cache
        self.snapshot_id = snapshot_id
        self.max_pois = max(2, int(max_pois))
        self._places = {
            str(place.get("place_id") or ""): place
            for place in world.get("places", [])
            if place.get("place_id")
        }
        self.last_added: list[dict[str, Any]] = []
        if self.path.exists():
            payload = read_json(self.path)
            self.graph_mode = str(payload.get("graph_mode") or "dense")
            loaded_ids = [
                place_id for place_id in payload.get("place_ids", [])
                if place_id in self._places
            ]
            # Lowering the configured cap stops future expansion; it never
            # truncates already frozen POIs or their route facts.
            self.place_ids = loaded_ids
            if int(payload.get("max_pois") or self.DEFAULT_MAX_POIS) != self.max_pois:
                self.save(initialized_from_legacy_cache=bool(payload.get("initialized_from_legacy_cache")))
            return

        self.place_ids = self._select_legacy_pool(route_cache)
        self.graph_mode = "legacy_sparse" if self.place_ids else "dense"
        self.save(initialized_from_legacy_cache=bool(self.place_ids))

    @property
    def places(self) -> list[dict[str, Any]]:
        return [self._places[place_id] for place_id in self.place_ids if place_id in self._places]

    @property
    def place_id_set(self) -> set[str]:
        return set(self.place_ids)

    def add(self, places: list[dict[str, Any]]) -> list[dict[str, Any]]:
        added: list[dict[str, Any]] = []
        existing = self.place_id_set
        for place in places:
            place_id = str(place.get("place_id") or "")
            if not place_id or place_id in existing or place_id not in self._places:
                continue
            if len(self.place_ids) >= self.max_pois:
                break
            self.place_ids.append(place_id)
            existing.add(place_id)
            added.append(self._places[place_id])
        if added:
            self.last_added = added
            self.save(initialized_from_legacy_cache=False)
        else:
            self.last_added = []
        return added

    def save(self, *, initialized_from_legacy_cache: bool) -> None:
        write_json(self.path, {
            "schema_version": 1,
            "snapshot_id": self.snapshot_id,
            "max_pois": self.max_pois,
            "graph_mode": self.graph_mode,
            "initialized_from_legacy_cache": initialized_from_legacy_cache,
            "place_ids": self.place_ids,
        })

    def _select_legacy_pool(self, route_cache: Any) -> list[str]:
        coordinate_to_id = {
            (
                _normalized_coordinate(place.get("location", {}).get("lat")),
                _normalized_coordinate(place.get("location", {}).get("lng")),
            ): place_id
            for place_id, place in self._places.items()
        }
        candidates: set[str] = set()
        for entry in route_cache._cache.values():
            if (entry.get("snapshot_id") or "static_v1") != self.snapshot_id:
                continue
            origin = str(entry.get("origin_place_id") or "")
            destination = str(entry.get("dest_place_id") or "")
            if origin not in self._places:
                origin = coordinate_to_id.get((
                    _normalized_coordinate(entry.get("origin_lat")),
                    _normalized_coordinate(entry.get("origin_lng")),
                ), "")
            if destination not in self._places:
                destination = coordinate_to_id.get((
                    _normalized_coordinate(entry.get("dest_lat")),
                    _normalized_coordinate(entry.get("dest_lng")),
                ), "")
            if origin and destination and origin != destination:
                candidates.update((origin, destination))
        return sorted(candidates)
