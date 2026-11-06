from __future__ import annotations
import hashlib
import logging
from pathlib import Path
from typing import Any
from shared.io import read_json

def _resolve_route_cache(path: str | None, tasks_dir: str | Path) -> str:
    """Auto-discover route_cache.json from tasks path.
    New structure: benchmarks/{task_type}/{city_id}/tasks/
    """
    if path and Path(path).exists():
        return path
    tasks_dir = Path(tasks_dir)
    parts = list(tasks_dir.parts)
    city_id = ''
    for (i, p) in enumerate(parts):
        if p != 'benchmarks':
            continue
        if i + 5 < len(parts) and parts[i + 5] == 'tasks':
            city_id = parts[i + 3]
        elif i + 3 < len(parts):
            city_id = parts[i + 2]
            break
    provenance = _task_batch_provenance(tasks_dir)
    world_id = provenance.get('world_id', '')
    required_versions = provenance.get('required_city_versions', {})
    if city_id and world_id:
        version = required_versions.get(city_id)
        if version:
            world_bound = Path('outputs', 'worlds', world_id, 'artifacts', 'routes', city_id, f'v{int(version)}', 'route_cache.json')
            if world_bound.exists():
                return str(world_bound)
        return ''
    if city_id:
        candidates = [f'outputs/routes/{city_id}/route_cache.json', f"outputs/routes/{city_id.replace('_smoke', '')}/route_cache.json"]
        for c in candidates:
            if Path(c).exists():
                return c
    return ''

def _resolve_place_world_snapshot(path: str | None, task_dir: str | Path, task_data: dict[str, Any]) -> dict[str, Any]:
    """Resolve the complete, version-frozen world visible to a place task.

    Provenance binds internal evaluation data; it must not narrow the Agent's
    candidate space to the task's gold city.  Managed place tasks therefore
    receive every city listed in ``world_city_versions``, each from the exact
    recorded city version.  The optional ``path`` remains a legacy escape
    hatch only when no managed provenance exists.
    """
    task_dir = Path(task_dir)
    provenance_path = task_dir / 'task_provenance.json'
    provenance: dict[str, Any] = {}
    if provenance_path.is_file():
        provenance = read_json(provenance_path)
    else:
        batch_provenance = task_dir.parent.parent / 'world_provenance.json'
        if batch_provenance.is_file():
            provenance = read_json(batch_provenance)
    context = provenance.get('generation_context') if isinstance(provenance.get('generation_context'), dict) else provenance
    world_id = str(provenance.get('world_id') or '')
    city_id = str(provenance.get('city_id') or task_data.get('city_id') or context.get('city_id') or '')
    versions = context.get('world_city_versions') or context.get('required_city_versions') or {}
    if not versions and city_id and context.get('city_version'):
        versions = {city_id: context['city_version']}
    if world_id and city_id and isinstance(versions, dict) and versions:
        frozen_snapshot = context.get('world_map_snapshot') or {}
        paths: list[str] = []
        route_cache_paths: list[str] = []
        local_service_world_state_paths: list[str] = []
        place_alias_cache_paths: list[str] = []
        place_alias_cache_hashes: dict[str, str] = {}
        local_service_state_hashes: dict[str, str] = {}
        hashes: dict[str, str] = {}
        normalized_versions: dict[str, int] = {}
        for (bound_city, version_value) in sorted(versions.items()):
            try:
                version = int(str(version_value).lstrip('vV'))
            except (TypeError, ValueError) as exc:
                raise ValueError(f'Invalid city version in place-task provenance: {bound_city}={version_value!r}') from exc
            registry_path = Path('outputs', 'worlds', world_id, 'cities', f'{bound_city}.json')
            if not registry_path.is_file():
                raise FileNotFoundError(f'Place-task city registry is missing: {registry_path}')
            registry = read_json(registry_path)
            matched = next((item for item in registry.get('city_versions', []) if item.get('version') == version), None)
            artifacts = (matched or {}).get('artifacts') or {}
            artifact = artifacts.get('map_world') or {}
            artifact_path = Path(str(artifact.get('path') or ''))
            if not artifact_path.is_file():
                raise FileNotFoundError(f'Place-task map artifact is missing for {world_id}/{bound_city}/v{version}: {artifact_path}')
            actual_hash = _sha256_file(artifact_path)
            snapshot_entry = frozen_snapshot.get(bound_city) if isinstance(frozen_snapshot, dict) else {}
            expected_hash = str((snapshot_entry or {}).get('sha256') or '')
            if not expected_hash and bound_city == city_id:
                expected_hash = str((context.get('artifact_snapshot') or {}).get('map_world') or '')
            if not expected_hash:
                expected_hash = str(artifact.get('sha256') or '')
            if expected_hash and actual_hash != expected_hash:
                raise ValueError(f'Place-task map artifact hash mismatch for {world_id}/{bound_city}/v{version}: expected {expected_hash}, got {actual_hash}')
            paths.append(str(artifact_path))
            hashes[str(bound_city)] = actual_hash
            if task_data.get('task_type') == 'place_discovery':
                route_artifact = artifacts.get('route_cache') or {}
                route_path = Path(str(route_artifact.get('path') or ''))
                if route_artifact.get('status') == 'available' and route_path.is_file():
                    task_route_hash = (context.get('artifact_snapshot') or {}).get('route_cache') if bound_city == city_id else None
                    expected_route_hash = task_route_hash or route_artifact.get('sha256')
                    if expected_route_hash and _sha256_file(route_path) == expected_route_hash:
                        route_cache_paths.append(str(route_path))
                    else:
                        logging.warning('Ignoring unverified Discovery route cache: %s', route_path)
            service_state_artifact = artifacts.get('local_service_world_state') or {}
            service_state_path = Path(str(service_state_artifact.get('path') or ''))
            if service_state_artifact.get('status') == 'available' and service_state_path.is_file():
                state_hash = _sha256_file(service_state_path)
                expected_state_hash = str(service_state_artifact.get('sha256') or '')
                if expected_state_hash and state_hash != expected_state_hash:
                    raise ValueError(f'LocalServiceApp world-state hash mismatch for {world_id}/{bound_city}/v{version}: expected {expected_state_hash}, got {state_hash}')
                local_service_world_state_paths.append(str(service_state_path))
                local_service_state_hashes[str(bound_city)] = state_hash
            alias_artifact = artifacts.get('place_alias_cache') or {}
            alias_path = Path(str(alias_artifact.get('path') or ''))
            if alias_artifact.get('status') == 'available' and alias_path.is_file():
                alias_hash = _sha256_file(alias_path)
                expected_alias_hash = str(alias_artifact.get('sha256') or '')
                if expected_alias_hash and alias_hash != expected_alias_hash:
                    raise ValueError(f'MapApp alias-cache hash mismatch for {world_id}/{bound_city}/v{version}: expected {expected_alias_hash}, got {alias_hash}')
                place_alias_cache_paths.append(str(alias_path))
                place_alias_cache_hashes[str(bound_city)] = alias_hash
            normalized_versions[str(bound_city)] = version
        return {'world_id': world_id, 'city_versions': normalized_versions, 'world_paths': paths, 'route_cache_paths': route_cache_paths, 'world_hashes': hashes, 'local_service_world_state_paths': local_service_world_state_paths, 'local_service_world_state_hashes': local_service_state_hashes, 'place_alias_cache_paths': place_alias_cache_paths, 'place_alias_cache_hashes': place_alias_cache_hashes}
    if path and Path(path).exists():
        return {'world_id': 'legacy', 'city_versions': {}, 'world_paths': [str(path)], 'world_hashes': {}, 'local_service_world_state_paths': [], 'local_service_world_state_hashes': {}, 'place_alias_cache_paths': [], 'place_alias_cache_hashes': {}}
    city_id = task_data.get('city_id', '')
    if not city_id:
        parts = list(task_dir.parts)
        for (i, part) in enumerate(parts):
            if part != 'benchmarks':
                continue
            if i + 5 < len(parts) and parts[i + 5] == 'tasks':
                city_id = parts[i + 3]
            elif i + 3 < len(parts):
                city_id = parts[i + 2]
                break
    candidates = []
    if city_id:
        candidates.extend([f'outputs/mapseeds/{city_id}/base/world.json', f'outputs/mapseeds/{city_id}/complex/world.json', f"outputs/mapseeds/{city_id.replace('_smoke', '')}/base/world.json", f"outputs/mapseeds/{city_id.replace('_smoke', '')}/complex/world.json"])
    for candidate in candidates:
        if Path(candidate).exists():
            return {'world_id': 'legacy', 'city_versions': {}, 'world_paths': [candidate], 'world_hashes': {}, 'local_service_world_state_paths': [], 'local_service_world_state_hashes': {}, 'place_alias_cache_paths': [], 'place_alias_cache_hashes': {}}
    raise FileNotFoundError(f"No world.json found for place task {task_data.get('task_id', task_dir.name)!r}. Pass --world outputs/mapseeds/<city>/base/world.json.")

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda : handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

def _task_batch_provenance(tasks_dir: Path) -> dict[str, Any]:
    """Read the immutable world binding stored beside a generated task batch."""
    path = tasks_dir.parent / 'world_provenance.json'
    if not path.is_file():
        return {'path': '', 'world_id': '', 'required_city_versions': {}}
    try:
        payload = read_json(path)
    except Exception:
        return {'path': str(path), 'world_id': '', 'required_city_versions': {}}
    versions = payload.get('required_city_versions') or {}
    if not isinstance(versions, dict):
        versions = {}
    normalized_versions: dict[str, int] = {}
    for (city_id, version) in versions.items():
        try:
            normalized_versions[str(city_id)] = int(version)
        except (TypeError, ValueError):
            continue
    return {'path': str(path), 'world_id': str(payload.get('world_id') or ''), 'required_city_versions': normalized_versions}
