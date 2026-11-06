from __future__ import annotations
import random
from typing import Any

class RouteSampler:
    """Samples POIs and generates route pairs for route planning tasks."""
    DISTANCE_TIERS = {'short': (2000, 5000), 'medium': (5000, 15000), 'long': (15000, 30000)}
    MODE_FOR_DISTANCE = {'short': ['TRANSIT', 'DRIVE', 'WALK'], 'medium': ['TRANSIT', 'DRIVE'], 'long': ['TRANSIT', 'DRIVE']}

    def __init__(self, world: dict[str, Any], characters: dict[str, Any], rng: random.Random | None=None):
        self.world = world
        self.characters = characters
        self.rng = rng or random.Random()
        self.places = world.get('places', [])
        self.districts = world.get('districts', [])
        self.place_to_district: dict[str, str] = {}
        self.district_to_places: dict[str, list[dict]] = {}
        for p in self.places:
            did = p.get('district_id', '')
            pid = p.get('place_id', '')
            if did:
                self.place_to_district[pid] = did
                self.district_to_places.setdefault(did, []).append(p)
        self.district_coords: dict[str, tuple[float, float]] = {}
        for p in self.places:
            loc = p.get('location', {})
            did = p.get('district_id', '')
            if did and did not in self.district_coords and loc.get('lat'):
                self.district_coords[did] = (loc['lat'], loc['lng'])
