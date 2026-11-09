"""Read-only type supplementation with explicit snapshot evidence for review drafts."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path


def matches_place_type(venue, expected):
    """Match a frozen place classification without inferring unstored types."""
    if not expected:
        return False
    if venue.get('primary_type') == expected:
        return True
    types = venue.get('types') or (venue.get('map_projection') or {}).get('types')
    return isinstance(types, list) and expected in types


def supplement_types(source, venues):
    source = Path(source)
    provenance = json.loads((source / 'task_provenance.json').read_text(encoding='utf-8'))
    city = provenance['city_id']
    snapshot = provenance['generation_context']['world_map_snapshot'][city]
    root = next(p for p in source.parents if (p / 'SYSTEM_DESIGN.md').exists())
    path = root / snapshot['path']
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    world = json.loads(raw)
    places = {p['place_id']: p for p in world['places']}
    result = deepcopy(venues)
    for venue in result:
        place = places[venue['place_id']]
        if place['name'] != venue['name'] or place.get('primary_type') != venue.get('primary_type'):
            raise ValueError('Type source identity or primary type differs from task')
        location = place.get('location', {})
        if location != venue.get('location', {}):
            raise ValueError('Type source coordinates differ from task')
        types = (place.get('map_projection') or {}).get('types') or place.get('types')
        if not isinstance(types, list) or not types:
            raise ValueError('Complete Google type evidence missing')
        venue['types'] = list(types)
    evidence = {'path': str(path), 'sha256': digest, 'recorded_sha256': snapshot['sha256'],
                'snapshot_matches_original': digest == snapshot['sha256'],
                'scope': 'review draft only; original provenance not rebound',
                'candidate_places': result}
    return result, evidence
