"""Task session loading for benchmark execution."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from shared.io import read_json

@dataclass
class TaskSession:
    """All files needed to run and judge one task."""
    folder: str
    task_dir: Path
    task: dict[str, Any]
    judge_sequence: dict[str, Any]
    world_slice: dict[str, Any]
    gold: dict[str, Any]
    curated_source: dict[str, Any]
    world: dict[str, Any]

    @property
    def task_id(self) -> str:
        return self.task.get('task_id', self.task_dir.name)

    @property
    def task_type(self) -> str:
        return self.task.get('task_type', '')

def load_session(task_dir: Path, folder: str, world_path: str | Path | dict[str, Any]) -> TaskSession:
    task = read_json(task_dir / 'task.json')
    judge_sequence = read_json(task_dir / 'judge_sequence.json')
    judge_sequence = _normalize_legacy_grounding_judge(task, judge_sequence)
    world_slice = read_json(task_dir / 'world_slice.json')
    curated_source_path = task_dir / 'curated_source.json'
    curated_source = read_json(curated_source_path) if curated_source_path.exists() else {}
    gold = _read_gold(task_dir, task.get('task_type', ''))
    world = _load_mapapp_world(world_path)
    binding = world_path if isinstance(world_path, dict) else {}
    expected_cities = set((binding.get('city_versions') or {}).keys())
    actual_cities = {str(city.get('city_id') or '') for city in world.get('cities', []) if isinstance(city, dict)}
    if expected_cities and actual_cities != expected_cities:
        raise ValueError(f'Bound MapApp world scope mismatch: expected {sorted(expected_cities)!r}, got {sorted(actual_cities)!r}')
    return TaskSession(folder=folder, task_dir=task_dir, task=task, judge_sequence=judge_sequence, world_slice=world_slice, gold=gold, curated_source=curated_source, world=world)

def _normalize_legacy_grounding_judge(task: dict[str, Any], judge_sequence: dict[str, Any]) -> dict[str, Any]:
    """Apply current exact-POI semantics to immutable legacy task artifacts.

    Early batches recorded a 300 m diagnostic tolerance alongside an exact POI
    candidate ID. Exact grounding is now, and has always been intended to be,
    identity-based.  Keep old task evidence unchanged while ensuring the runner
    never executes the obsolete spatial check.
    """
    summary = task.get('summary') if isinstance(task.get('summary'), dict) else {}
    granularity = task.get('acceptable_granularity') or summary.get('acceptable_granularity')
    if task.get('task_type') != 'place_grounding' or granularity != 'exact_poi':
        return judge_sequence
    normalized = deepcopy(judge_sequence)
    for stage in normalized.get('stages', []):
        if not isinstance(stage, dict):
            continue
        for node in stage.get('nodes', []):
            if not isinstance(node, dict):
                continue
            for field in ('hard_checks', 'soft_checks'):
                checks = node.get(field)
                if isinstance(checks, list):
                    node[field] = [check for check in checks if not isinstance(check, dict) or check.get('check_type') != 'geo_within_radius']
    return normalized

def _load_mapapp_world(world_path: str | Path | dict[str, Any]) -> dict[str, Any]:
    """Load only the explicitly provenance-bound city artifacts.

    The controlled merge creates a frozen full-world search space. It never
    scans mapseed directories and therefore cannot introduce unrelated cities.
    """
    raw_paths = world_path.get('world_paths', []) if isinstance(world_path, dict) else [world_path]
    worlds: list[dict[str, Any]] = []
    for raw_path in raw_paths:
        path = Path(raw_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f'Bound MapApp world is missing: {path}')
        world = read_json(path)
        world['_source_world_path'] = str(path)
        worlds.append(world)
    aliases = _load_place_aliases(world_path.get('place_alias_cache_paths', []) if isinstance(world_path, dict) else [])
    for world in worlds:
        for place in world.get('places', []):
            place_aliases = aliases.get(str(place.get('place_id') or ''), [])
            if place_aliases:
                projection = place.setdefault('map_projection', {})
                projection['aliases'] = list(dict.fromkeys([*(projection.get('aliases') or []), *place_aliases]))
    if not worlds:
        return {}
    if len(worlds) == 1:
        return worlds[0]
    return _merge_bound_worlds(worlds)

def _load_place_aliases(paths: list[str]) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    for raw_path in paths:
        data = read_json(Path(raw_path))
        for item in data.get('aliases', []):
            if not isinstance(item, dict) or item.get('status') != 'available':
                continue
            place_id = str(item.get('place_id') or '')
            english_name = str(item.get('english_display_name') or '').strip()
            if place_id and english_name:
                aliases.setdefault(place_id, []).append(english_name)
        for item in data.get('verified_query_aliases', []):
            if not isinstance(item, dict) or item.get('status') != 'available':
                continue
            place_id = str(item.get('place_id') or '')
            query = str(item.get('query') or '').strip()
            if place_id and query:
                aliases.setdefault(place_id, []).append(query)
    return aliases

def _merge_bound_worlds(worlds: list[dict[str, Any]]) -> dict[str, Any]:
    return {'schema_version': 'geoword.mapapp.frozen_world_snapshot.v1', 'cities': _dedupe_records(worlds, 'cities', 'city_id'), 'districts': _dedupe_records(worlds, 'districts', 'district_id'), 'places': _dedupe_records(worlds, 'places', 'place_id'), 'services': _dedupe_records(worlds, 'services', 'service_id'), 'source_world_paths': [world.get('_source_world_path', '') for world in worlds]}

def _dedupe_records(worlds: list[dict[str, Any]], key: str, id_key: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for world in worlds:
        for item in world.get(key, []):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get(id_key) or '')
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            records.append(dict(item))
    return records

def resolve_task_dir(manifest_path: str | Path, folder: str) -> Path:
    manifest_dir = Path(manifest_path).resolve().parent
    normalized = folder.replace('\\', '/').lstrip('/')
    if normalized == 'tasks' or normalized.startswith('tasks/'):
        return manifest_dir / normalized
    return manifest_dir / 'tasks' / normalized

def _read_gold(task_dir: Path, task_type: str) -> dict[str, Any]:
    name = 'grounding_gold.json' if task_type == 'place_grounding' else 'discovery_gold.json'
    path = task_dir / name
    return read_json(path) if path.exists() else {}
