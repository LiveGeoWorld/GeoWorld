"""Read only the task city's version-bound map for private identity resolution."""
from __future__ import annotations
import hashlib
import json
from functools import lru_cache
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]

@lru_cache(maxsize=12)
def _decode(raw: bytes):
    return json.loads(raw)

def load_identity_places(task_dir: str | Path):
    task_dir = Path(task_dir)
    provenance_path = task_dir / 'task_provenance.json'
    if not provenance_path.is_file():
        provenance_path = task_dir.parent.parent / 'world_provenance.json'
    provenance = json.loads(provenance_path.read_text())
    context = provenance.get('generation_context') or provenance
    world = provenance.get('world_id')
    city = provenance.get('city_id') or context.get('city_id')
    versions = context.get('world_city_versions') or context.get('required_city_versions') or {}
    version = versions.get(city, context.get('city_version'))
    if not world or not city or version is None:
        raise ValueError('Itinerary identity requires frozen world/city/version provenance')
    version = int(str(version).lstrip('vV'))
    registry_path = ROOT / 'outputs' / 'worlds' / world / 'cities' / (city + '.json')
    registry = json.loads(registry_path.read_text())
    if registry.get('city_id') != city:
        raise ValueError('Itinerary identity city registry mismatch')
    bound = next((item for item in registry.get('city_versions', []) if item.get('version') == version), None)
    artifact = ((bound or {}).get('artifacts') or {}).get('map_world') or {}
    snapshot = (context.get('world_map_snapshot') or {}).get(city) or {}
    if snapshot.get('version') is not None and int(str(snapshot['version']).lstrip('vV')) != version:
        raise ValueError('Itinerary identity snapshot version mismatch')
    if not artifact.get('path'):
        raise ValueError('Itinerary identity bound map version is unavailable')
    path = Path(artifact['path'])
    if not path.is_absolute():
        path = ROOT / path
    expected = snapshot.get('sha256') or (context.get('artifact_snapshot') or {}).get('map_world') or artifact.get('sha256')
    if not expected:
        raise ValueError('Itinerary identity frozen map hash is missing')
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError('Itinerary identity frozen map hash mismatch')
    data = _decode(raw)
    places = [p for p in data.get('places', []) if p.get('city_id') == city]
    if not places:
        raise ValueError('Itinerary identity bound city has no map places')
    return (places, {'world_id': world, 'city_id': city, 'city_version': version, 'path': str(path), 'sha256': actual, 'place_count': len(places)})
