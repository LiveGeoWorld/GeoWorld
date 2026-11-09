"""Existing task-review and author-map handlers, mounted in World Studio."""
from __future__ import annotations
import hashlib,json,os
from datetime import datetime
from pathlib import Path
from shared.io import read_json,write_json
from world_module.registry import resolve_world_route_cache
from test_runner.verification.shared.task_contract import validate_task_folder
from vis.tasks.app_http import AppHTTP
from vis.tasks.server import ROOT as PIPELINE_ROOT,send,task_folder
WORKSPACE_ROOT=PIPELINE_ROOT
ROUTE_SANDBOX_TYPES = {'route_planning'}
DUAL_WRITE_TYPES = {'itinerary', 'safety_planning', 'region_aware', 'dynamic_replanning', 'route_planning', 'multi_entity'}
EDITABLE_TASK_FIELDS = {'place_grounding': {'user_prompt'}, 'place_discovery': {'user_prompt'}, 'route_planning': {'correct_route_id', 'correct_leg_route_ids', 'correct_route_summary', 'expected_feasible', 'user_prompt'}, 'multi_entity': {'expected_restaurant_service_id', 'expected_feasible', 'user_prompt', 'constraints'}, 'itinerary': {'correct_itinerary', 'expected_feasible', 'user_prompt', 'constraints'}, 'safety_planning': {'expected_safe', 'risk_factors', 'expected_plan', 'constraints', 'user_prompt'}, 'region_aware': {'expected_feasible', 'user_prompt', 'expected_plan', 'wrong_plans', 'explanation_keywords'}, 'dynamic_replanning': {'user_prompt'}}
_route_cache_store: dict[str, dict] = {}
_route_cache_stamps: dict[str, tuple] = {}

def _get_route_cache(cache_path: Path) -> dict:
    """Load and cache route data, normalizing list→dict by route_id."""
    key = str(cache_path)
    from .dynamic_fingerprints import stamp
    revision = stamp(cache_path)
    if key in _route_cache_store and _route_cache_stamps.get(key) == revision:
        return _route_cache_store[key]
    data = read_json(cache_path)
    routes_raw = data.get('cache') or data.get('routes', {})
    if isinstance(routes_raw, list):
        routes = {r.get('route_id', ''): r for r in routes_raw}
    else:
        routes = routes_raw
    cached = {'routes': routes, 'raw': data}
    _route_cache_store[key] = cached
    _route_cache_stamps[key] = revision
    return cached



def inject_bound_route_evidence(task_dir: Path, sandbox: dict) -> None:
    """Resolve the review evidence from the same managed route cache as evaluation."""
    provenance = task_dir / 'task_provenance.json'
    batch_provenance = task_dir.parent.parent / 'world_provenance.json'
    record = read_json(provenance) if provenance.is_file() else (
        read_json(batch_provenance) if batch_provenance.is_file() else {}
    )
    context = record.get('generation_context') or record
    world = record.get('world_id') or context.get('world_id')
    city = record.get('city_id') or context.get('city_id')
    version = context.get('city_version') or (record.get('required_city_versions') or {}).get(city)
    if world and city and version:
        _inject_gt_routes(sandbox, resolve_world_route_cache(world, city, version))


def _inject_gt_routes(sb: dict, route_cache: Path | None) -> None:
    """Inject gt_route/gt_segments from route_cache (like per-type route_planning vis_server)."""
    if not route_cache or not route_cache.exists():
        return
    cached = _get_route_cache(route_cache)
    routes = cached["routes"]

    def _enrich(r):
        # Resolve only within this bound cache; keep the referring identity.
        r = dict(r)
        source = r
        seen = {r.get("route_id")}
        while not source.get("route") and source.get("route_ref"):
            ref = source["route_ref"]
            if ref in seen or ref not in routes:
                break
            seen.add(ref)
            source = routes[ref]
        if source.get("route") and not r.get("route"):
            r["route"] = source["route"]
        if r.get("polyline_encodings"): return r
        encodings = []
        route_data = (r.get("route", {}) or {})
        steps = route_data.get("steps", []) or []
        for step in steps:
            enc = (step.get("polyline", {}) or {}).get("encodedPolyline", "")
            if enc: encodings.append(enc)
        if encodings:
            r["polyline_encodings"] = encodings
            r.setdefault("distance_km", (route_data.get("distance_m", 0) or 0) / 1000)
            r.setdefault("duration_min", (route_data.get("duration_s", 0) or 0) / 60)
        return r

    rid = sb.get("correct_route_id", "")
    if rid and rid in routes:
        sb["gt_route"] = _enrich(routes[rid])
    leg_ids = sb.get("correct_leg_route_ids", []) or []
    segments = []
    if leg_ids:
        for lid in leg_ids:
            if lid and lid in routes:
                segments.append(_enrich(routes[lid]))
        if segments:
            sb["gt_segments"] = segments
    # Supplement all_routes_in_task
    all_routes = [_enrich(routes.get(r.get("route_id"), r)) for r in (sb.get("all_routes_in_task", []) or [])]
    existing_ids = {r.get("route_id", "") for r in all_routes}
    core_place_ids = {p.get("place_id") for p in (sb.get("core_places", []) or [])}
    for rid, r in routes.items():
        if rid in existing_ids:
            continue
        if r.get("origin_place_id") in core_place_ids or r.get("dest_place_id") in core_place_ids:
            all_routes.append(_enrich({**r, "route_id": rid}))
            existing_ids.add(rid)
    if all_routes:
        sb["all_routes_in_task"] = all_routes



def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda : handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()

def _world_places(world: dict) -> list[dict]:
    places = list(world.get('places', []) or [])
    seen = {p.get('place_id') or p.get('google_place_id') or p.get('source_place_id') for p in places}
    for district in world.get('districts', []) or []:
        for place in (district.get('director_notes', {}) or {}).get('anchor_places', []) or []:
            pid = place.get('place_id') or place.get('google_place_id') or place.get('source_place_id')
            if pid in seen:
                continue
            clone = dict(place)
            clone.setdefault('place_id', pid)
            clone.setdefault('district_id', district.get('district_id', ''))
            places.append(clone)
            seen.add(pid)
    return places

class WorkbenchHTTP(AppHTTP):

    def __init__(self, handler, benchmarks_root, params):
        super().__init__(handler, benchmarks_root)
        self.params = params
        self._target = None
        self.manifest = {'city_id': params.get('city') or (getattr(handler, 'map_scope', {}) or {}).get('city_id', '')}
        if not self.manifest['city_id']:
            cities = self._current_world_cities()
            if len(cities) == 1:
                self.manifest['city_id'] = cities[0]['city_id']

    def _resolve_task_folder(self, folder):
        if self._target is not None and folder == self._target.name:
            return self._target
        return task_folder(Path(self.benchmarks_root), folder)

    def _read_json_body(self):
        try:
            length = int(self.handler.headers.get('Content-Length', '0'))
            data = json.loads(self.handler.rfile.read(length).decode('utf-8')) if length else {}
            if not isinstance(data, dict):
                raise ValueError('Expected a JSON object.')
            return data
        except (ValueError, UnicodeError) as exc:
            self._error(str(exc), 400)
            return None

    def _resolve_world_paths(self):
        world = self._current_browsing_world_id()
        result = []
        for city in self._current_world_cities():
            artifacts = self._active_city_artifacts(world, city['city_id'], city['active_version'])
            path = self._registered_artifact_path(artifacts, 'map_world')
            if path is not None:
                result.append(path.resolve())
        return result

    def handle(self, method, path):
        if method == 'POST':
            folder = self.params.get('folder', '')
            if not folder:
                self._error('Missing folder parameter.', 400)
                return
            self._target = self._resolve_task_folder(folder)
            actions = {'/api/task/save': self._save_task, '/api/task/review': self._review_task, '/api/task/source': self._save_source, '/api/task/judge': self._save_judge}
            actions[path](self._target.name)
        elif path == '/api/world/points':
            self._serve_world_points(self.params.get('folder', ''))
        elif path == '/api/world/full':
            self._serve_world_full()
        elif path == '/api/route_cache':
            self._serve_route_cache()
        elif path == '/api/routes':
            self._serve_routes_query({k: [v] for (k, v) in self.params.items()})
        elif path == '/api/cached_places':
            self._serve_cached_places()

    def _resolve_route_cache(self, city_id: str='') -> Path | None:
        manifest = self.manifest or {}
        city_id = city_id or manifest.get('city_id', '')
        world_id = self._current_browsing_world_id()
        if world_id and city_id:
            world_bound = resolve_world_route_cache(world_id, city_id)
            return world_bound
        if city_id:
            candidates = [PIPELINE_ROOT / 'outputs' / 'routes' / city_id / 'route_cache.json', PIPELINE_ROOT / 'outputs' / 'routes' / f'{city_id}_smoke' / 'route_cache.json', PIPELINE_ROOT / 'outputs' / 'routes' / f'{city_id}_complex' / 'route_cache.json']
            for c in candidates:
                if c.exists():
                    return c
        base = PIPELINE_ROOT / 'outputs' / 'routes'
        if base.exists():
            for child in sorted(base.iterdir(), reverse=True):
                cache = child / 'route_cache.json'
                if cache.exists():
                    return cache
        return None

    @staticmethod
    def _find_review_file(tasks_dir: Path) -> Path:
        """Find or create review_status.json at the batch level."""
        review = tasks_dir.parent / 'review_status.json'
        if review.parent == tasks_dir.parent == tasks_dir:
            review = tasks_dir / 'review_status.json'
        return review

    def _save_judge(self, task_folder: str) -> None:
        """Persist an edited judge sequence to the task folder and its source."""
        payload = self._read_json_body()
        if payload is None:
            return
        if not isinstance(payload.get('stages'), list):
            self._error("Body must contain a 'stages' list", 400)
            return
        base_dir = self._resolve_task_folder(task_folder)
        if not base_dir.exists():
            self._error(f'Task folder not found: {task_folder}', 404)
            return
        payload.setdefault('schema_version', 'curated.judge.v1')
        payload.setdefault('completion_policy', 'key_nodes')
        judge_path = base_dir / 'judge_sequence.json'
        judge_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        source_synced = self._sync_judge_to_source(base_dir, payload)
        self._json({'ok': True, 'task_judge': str(judge_path), 'source_synced': source_synced})

    def _save_source(self, task_folder: str) -> None:
        """Persist edited curated example fields back to source and task files."""
        payload = self._read_json_body()
        if payload is None:
            return
        example_patch = payload.get('example')
        if not isinstance(example_patch, dict):
            self._error("Body must contain an 'example' object", 400)
            return
        base_dir = self._resolve_task_folder(task_folder)
        if not base_dir.exists():
            self._error(f'Task folder not found: {task_folder}', 404)
            return
        source_file = base_dir / 'curated_source.json'
        if not source_file.exists():
            self._error('Task has no curated_source.json to sync', 400)
            return
        source = read_json(source_file)
        src_path = source.get('source_path', '')
        original = source.get('example') or {}
        example_id = original.get('example_id', '')
        if not src_path or not example_id:
            self._error('curated_source.json is missing source_path/example_id', 400)
            return
        src = Path(src_path)
        if not src.is_absolute():
            src = PIPELINE_ROOT / src
        src = src.resolve()
        if not src.exists() or not _is_relative_to(src, WORKSPACE_ROOT):
            self._error(f'Source file is not writable: {src}', 400)
            return
        merged = dict(original)
        merged.update(example_patch)
        merged['example_id'] = example_id
        data = read_json(src)
        changed = False
        for (index, example) in enumerate(data.get('examples', [])):
            if example.get('example_id') == example_id:
                data['examples'][index] = merged
                changed = True
                break
        if not changed:
            self._error(f'Example not found in source: {example_id}', 404)
            return
        src.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        source['example'] = merged
        source_file.write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding='utf-8')
        self._sync_source_to_task_files(base_dir, merged)
        self._json({'ok': True, 'source_path': str(src), 'example_id': example_id})

    def _save_task(self, task_folder: str) -> None:
        """Unified GT save — routes to correct sandbox file based on task_type."""
        payload = self._read_json_body()
        if payload is None:
            return
        task_type = payload.get('task_type', '')
        raw_fields = payload.get('fields') or {}
        if not isinstance(raw_fields, dict):
            self._error("Body must contain 'fields' dict", 400)
            return
        fields = dict(raw_fields)
        base_dir = self._resolve_task_folder(task_folder)
        if not base_dir.exists():
            self._error(f'Task folder not found: {task_folder}', 404)
            return
        task_path = base_dir / 'task.json'
        task = read_json(task_path) if task_path.exists() else {}
        actual_type = task.get('task_type', '')
        if not actual_type or task_type != actual_type:
            self._error(f"Task type mismatch: expected {actual_type or 'unknown'}, got {task_type or 'empty'}", 400)
            return
        allowed = EDITABLE_TASK_FIELDS.get(actual_type)
        if allowed is None:
            self._error(f'Task type is read-only in the unified editor: {actual_type}', 400)
            return
        unknown = sorted(set(fields) - allowed)
        if unknown:
            self._error(f"Unsupported editable fields for {actual_type}: {', '.join(unknown)}", 400)
            return
        sandbox_file = 'route_sandbox.json' if task_type in ROUTE_SANDBOX_TYPES else 'task_sandbox.json'
        if actual_type in {'place_grounding', 'place_discovery'} or (actual_type in {'route_planning', 'multi_entity'} and set(fields) == {'user_prompt'}):
            prompt = fields.get('user_prompt')
            if not isinstance(prompt, str) or not prompt.strip():
                self._error('User Prompt must be non-empty text', 400)
                return
            task['user_prompt'] = prompt
            write_json(task_path, task)
            for name in ('curated_source.json', 'task_spec.json', 'task_sandbox.json'):
                path = base_dir / name
                if not path.exists():
                    continue
                record = read_json(path)
                target = record.get('example') if name == 'curated_source.json' else record
                if isinstance(target, dict) and 'user_prompt' in target:
                    target['user_prompt'] = prompt
                    write_json(path, record)
            contract = validate_task_folder(base_dir, actual_type)
            review = self._mark_task_needs_fix(base_dir, task_folder, '题面已修改，需重新审核')
            self._json({'ok': True, 'contract': contract, 'review_status': review['status']})
            return
        sandbox_path = base_dir / sandbox_file
        sandbox = read_json(sandbox_path) if sandbox_path.exists() else {}
        if actual_type == 'safety_planning':
            from vis.tasks.safety_review import safety_edit_error
            error = safety_edit_error(sandbox, fields)
            if error:
                self._error(error, 400)
                return
        constraints = fields.pop('constraints', None)
        for (key, value) in fields.items():
            if isinstance(value, dict) and isinstance(sandbox.get(key), dict):
                sandbox[key] = {**sandbox[key], **value}
            else:
                sandbox[key] = value
        if isinstance(constraints, dict):
            existing = sandbox.setdefault('constraints', {})
            if isinstance(existing, dict):
                existing.update(constraints)
            else:
                sandbox['constraints'] = constraints
        elif constraints is not None:
            sandbox['constraints'] = constraints
        write_json(sandbox_path, sandbox)
        if task_type in DUAL_WRITE_TYPES and 'user_prompt' in fields:
            if task_path.exists():
                task['user_prompt'] = fields['user_prompt']
                write_json(task_path, task)
        contract = validate_task_folder(base_dir, actual_type)
        review = self._mark_task_needs_fix(base_dir, task_folder, '任务 GT 已在统一审核中心修改，需重新审核')
        self._json({'ok': True, 'sandbox': str(sandbox_path), 'contract': contract, 'review_status': review.get('status', 'needs_fix'), 'review_reason': review.get('reason', '')})

    def _mark_task_needs_fix(self, base_dir: Path, task_folder: str, reason: str) -> dict:
        """Invalidate prior approval after any GT edit made through the UI."""
        review_path = self._find_review_file(base_dir.parent)
        review = read_json(review_path) if review_path.exists() else {}
        entry = review.get(task_folder, {})
        entry.update({'status': 'needs_fix', 'reason': reason, 'reviewed_at': datetime.now().strftime('%Y-%m-%dT%H:%M:%S')})
        review[task_folder] = entry
        write_json(review_path, review)
        return entry

    def _review_task(self, task_folder: str) -> None:
        """Unified review — set approve/reject status for any task type."""
        payload = self._read_json_body()
        if payload is None:
            return
        status = payload.get('status')
        if status not in ('approved', 'rejected', 'pending', 'needs_fix'):
            self._error('status must be one of: approved, rejected, pending, needs_fix', 400)
            return
        base_dir = self._resolve_task_folder(task_folder)
        if not base_dir.exists():
            self._error(f'Task folder not found: {task_folder}', 404)
            return
        tasks_dir = base_dir.parent
        review_path = self._find_review_file(tasks_dir)
        review = read_json(review_path) if review_path.exists() else {}
        entry = review.get(task_folder, {})
        entry['status'] = status
        entry['reviewer'] = payload.get('reviewer', entry.get('reviewer', ''))
        entry['notes'] = payload.get('notes', entry.get('notes', ''))
        entry['reviewed_at'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        review[task_folder] = entry
        write_json(review_path, review)
        self._json({'ok': True, 'review_path': str(review_path), 'status': status})

    def _sync_source_to_task_files(self, base_dir: Path, example: dict) -> None:
        """Update immediately visible task files after a source edit."""
        task_path = base_dir / 'task.json'
        if task_path.exists():
            task = read_json(task_path)
            if 'user_prompt' in example:
                task['user_prompt'] = example['user_prompt']
            if 'difficulty' in example:
                task['difficulty'] = example['difficulty']
            summary = task.setdefault('summary', {})
            for key in ['review_status', 'prompt_language', 'review_language', 'user_prompt_translation', 'review_translation', 'service_type', 'start', 'ranking_criteria', 'difficulty_profile', 'quality_profile', 'quality_gate']:
                if key in example:
                    summary[key] = example[key]
            if 'quality_profile' in example and 'display_difficulty' in (example.get('quality_profile') or {}):
                summary['difficulty'] = example['quality_profile']['display_difficulty']
                task['difficulty'] = example['quality_profile']['display_difficulty']
            task_path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding='utf-8')
        task_type = example.get('task_type', 'place_grounding')
        gold_path = base_dir / ('grounding_gold.json' if task_type == 'place_grounding' else 'discovery_gold.json')
        if gold_path.exists() and isinstance(example.get('gold'), dict):
            gold_payload = {'example_id': example.get('example_id', ''), 'task_type': task_type, 'service_type': example.get('service_type', ''), 'start': example.get('start', {}), 'gold': example.get('gold', {}), 'evaluation': example.get('evaluation', {}), 'ranking_criteria': example.get('ranking_criteria', []), 'distractors': example.get('distractors', [])}
            existing_gold = read_json(gold_path)
            if "ambiguity_landscape" in example:
                gold_payload["ambiguity_landscape"] = example["ambiguity_landscape"]
            elif "ambiguity_landscape" in existing_gold:
                gold_payload["ambiguity_landscape"] = existing_gold["ambiguity_landscape"]
            gold_path.write_text(json.dumps(gold_payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def _sync_judge_to_source(self, base_dir: Path, payload: dict) -> bool:
        """Write the edited judge sequence back into the curated source file."""
        source_file = base_dir / 'curated_source.json'
        if not source_file.exists():
            return False
        source = read_json(source_file)
        src_path = source.get('source_path', '')
        example_id = (source.get('example') or {}).get('example_id', '')
        if not src_path or not example_id:
            return False
        src = Path(src_path)
        if not src.is_absolute():
            src = PIPELINE_ROOT / src
        src = src.resolve()
        if not src.exists() or not _is_relative_to(src, WORKSPACE_ROOT):
            return False
        data = read_json(src)
        changed = False
        for example in data.get('examples', []):
            if example.get('example_id') == example_id:
                example['judge_sequence_override'] = payload
                changed = True
                break
        if changed:
            src.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        return changed

    def _serve_world_points(self, task_folder: str='') -> None:
        """Serve POIs for a task's exact bound city-version when available.

        The generic mapseed fallback remains for the legacy visualizers that do
        not carry a task folder.  Task Studio must never use that fallback:
        its grounding map is evidence for one task, not the server's startup
        manifest.
        """
        binding: dict[str, object] = {}
        if task_folder:
            try:
                (world_path, binding) = self._resolve_task_bound_world(task_folder)
            except (FileNotFoundError, ValueError) as exc:
                self._error(str(exc), 409)
                return
            world_paths = [world_path]
        else:
            world_paths = self._resolve_world_paths()
        if not world_paths:
            self._json({'places': [], 'actors': [], 'world_path': '', 'world_paths': []})
            return
        places = []
        seen: set[str] = set()
        for world_path in world_paths:
            world = read_json(world_path)
            source_places = list(world.get('places') or []) if task_folder and world.get('places') else _world_places(world)
            for place in source_places:
                loc = place.get('location', {})
                if 'lat' not in loc or 'lng' not in loc:
                    continue
                place_id = place.get('place_id', '')
                if place_id and place_id in seen:
                    continue
                if place_id:
                    seen.add(place_id)
                places.append({'id': place_id, 'name': place.get('name', ''), 'type': place.get('primary_type', ''), 'city_id': place.get('city_id', ''), 'district_id': place.get('district_id', ''), 'lat': loc.get('lat'), 'lng': loc.get('lng')})
        self._json({'places': places, 'actors': [], 'world_path': str(world_paths[0]), 'world_paths': [str(path) for path in world_paths], **binding})

    def _resolve_task_bound_world(self, task_folder: str) -> tuple[Path, dict[str, object]]:
        """Resolve the immutable map snapshot recorded for one task folder."""
        task_dir = self._resolve_task_folder(task_folder)
        if not task_dir.is_dir():
            raise FileNotFoundError(f'Task folder not found: {task_folder}')
        task_provenance_path = task_dir / 'task_provenance.json'
        source = 'task_provenance'
        if task_provenance_path.is_file():
            provenance = read_json(task_provenance_path)
            context = provenance.get('generation_context') or {}
        else:
            source = 'batch_provenance'
            batch_provenance_path = task_dir.parent.parent / 'world_provenance.json'
            if not batch_provenance_path.is_file():
                raise FileNotFoundError(f'Task map binding is missing: {task_provenance_path.name} and {batch_provenance_path.name} were not found.')
            provenance = read_json(batch_provenance_path)
            contexts = provenance.get('generation_contexts') or []
            context = contexts[-1] if contexts and isinstance(contexts[-1], dict) else provenance
        if not isinstance(provenance, dict) or not isinstance(context, dict):
            raise ValueError('Task map binding is invalid: provenance must be a JSON object.')
        world_id = str(provenance.get('world_id') or context.get('world_id') or '')
        city_id = str(provenance.get('city_id') or context.get('city_id') or '')
        raw_version = context.get('city_version') or provenance.get('city_version') or ''
        version_text = str(raw_version).strip().lower().lstrip('v')
        try:
            city_version = int(version_text)
        except ValueError as exc:
            raise ValueError('Task map binding is invalid: city_version is required.') from exc
        if not world_id or not city_id or city_version < 1:
            raise ValueError('Task map binding is incomplete: world, city, and version are required.')
        record_path = PIPELINE_ROOT / 'outputs' / 'worlds' / world_id / 'cities' / f'{city_id}.json'
        if not record_path.is_file():
            raise FileNotFoundError(f'Registered city record not found for task map: {record_path}')
        record = read_json(record_path)
        version_entry = next((entry for entry in record.get('city_versions', []) if str(entry.get('version', '')).lstrip('v') == str(city_version)), None)
        if not isinstance(version_entry, dict):
            raise ValueError(f'Task map binding refers to unavailable {city_id} v{city_version}.')
        map_ref = (version_entry.get('artifacts') or {}).get('map_world') or {}
        map_path_value = str(map_ref.get('path') or '')
        map_path = Path(map_path_value)
        if not map_path.is_absolute():
            map_path = PIPELINE_ROOT / map_path
        map_path = map_path.resolve()
        world_root = (PIPELINE_ROOT / 'outputs' / 'worlds' / world_id).resolve()
        if not _is_relative_to(map_path, world_root) or not map_path.is_file():
            raise FileNotFoundError(f'Task map artifact is unavailable for {city_id} v{city_version}.')
        actual_hash = _sha256_file(map_path)
        registered_hash = str(map_ref.get('sha256') or '')
        snapshot = context.get('artifact_snapshot') or {}
        snapshot_hash = str(snapshot.get('map_world') or '') if isinstance(snapshot, dict) else ''
        if registered_hash and actual_hash != registered_hash:
            raise ValueError(f'Registered map artifact changed for {city_id} v{city_version}; refusing a fallback map.')
        if snapshot_hash and actual_hash != snapshot_hash:
            raise ValueError(f'Task map snapshot no longer matches {city_id} v{city_version}; refusing a fallback map.')
        return (map_path, {'world_id': world_id, 'city_id': city_id, 'city_name': str(record.get('city_name') or city_id), 'city_version': f'v{city_version}', 'map_sha256': actual_hash, 'source': source})

    def _serve_route_cache(self) -> None:
        cache = self._resolve_route_cache()
        if cache and cache.exists():
            self._json(read_json(cache))
        else:
            self._json({})

    def _serve_routes_query(self, params: dict) -> None:
        """GET /api/routes?from=X&to=Y[&mode=M][&waypoint=W] — filtered route query."""
        city = params.get('city', [None])[0]
        cache = self._resolve_route_cache(city or '')
        if not cache or not cache.exists():
            self._json({'routes': [], 'segments': []})
            return
        cached = _get_route_cache(cache)
        all_routes = cached['routes']
        origin = params.get('from', [None])[0]
        dest = params.get('to', [None])[0]
        mode_filter = params.get('mode', [None])[0]
        waypoint = params.get('waypoint', [None])[0]
        filtered = []
        if isinstance(all_routes, list):
            for r in all_routes:
                if origin and r.get('origin_place_id') != origin:
                    continue
                if dest and r.get('dest_place_id') != dest:
                    continue
                if mode_filter and mode_filter != 'ALL' and (mode_filter not in str(r.get('mode', ''))):
                    continue
                filtered.append(r)
        else:
            for (rid, r) in all_routes.items():
                if origin and r.get('origin_place_id') != origin:
                    continue
                if dest and r.get('dest_place_id') != dest:
                    continue
                if mode_filter and mode_filter != 'ALL' and (mode_filter not in str(r.get('mode', ''))):
                    continue
                filtered.append({**r, 'route_id': rid})
        segments = []
        if waypoint and origin and dest:
            wp_routes_leg1 = [r for r in filtered if r.get('dest_place_id') == waypoint]
            wp_routes_leg2 = [{**r, 'route_id': rid} for (rid, r) in all_routes.items() if r.get('origin_place_id') == waypoint and r.get('dest_place_id') == dest]
            segments = [{'leg': 1, 'routes': wp_routes_leg1, 'label': f'{origin}→{waypoint}'}, {'leg': 2, 'routes': wp_routes_leg2, 'label': f'{waypoint}→{dest}'}]
        self._json({'routes': filtered, 'segments': segments})

    def _serve_cached_places(self) -> None:
        """GET /api/cached_places — places that appear in route cache, sorted by route count."""
        cache = self._resolve_route_cache()
        if not cache or not cache.exists():
            self._json({'places': []})
            return
        cached = _get_route_cache(cache)
        routes_list = list(cached['routes'].values())
        place_counts: dict[str, int] = {}
        place_info: dict[str, dict] = {}
        for r in routes_list:
            for pid in [r.get('origin_place_id', ''), r.get('dest_place_id', '')]:
                if not pid:
                    continue
                if pid not in place_counts:
                    place_counts[pid] = 0
                    place_info[pid] = {'place_id': pid, 'name': r.get(f'origin_name' if pid == r.get('origin_place_id') else 'dest_name', pid), 'type': ''}
                place_counts[pid] += 1
        places = sorted([{**place_info[pid], 'cached_routes': c} for (pid, c) in place_counts.items()], key=lambda p: -p['cached_routes'])
        self._json({'places': places})

    def _serve_world_full(self) -> None:
        """GET /api/world/full — full world data with city info (all cities)."""
        world_paths = self._resolve_world_paths()
        if not world_paths:
            self._json({})
            return
        all_places = []
        all_districts = []
        city_info = {'name': '', 'city_id': ''}
        for wp in world_paths:
            world = read_json(wp)
            city = (world.get('cities') or [{}])[0]
            city_info = {'name': city.get('name', ''), 'city_id': city.get('city_id', '')}
            for p in _world_places(world):
                loc = p.get('location', {}) or {}
                p['lat'] = loc.get('lat')
                p['lng'] = loc.get('lng')
                all_places.append(p)
            all_districts.extend(world.get('districts', [])[:50])
        self._json({'city': city_info, 'places': all_places[:2000], 'districts': all_districts[:100]})
