"""Existing test-workbench execution and result endpoints, relocated verbatim."""
from __future__ import annotations
import copy
import http.client
import json
import math
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from shared.io import read_json, write_json
from shared.config_loader import PipelineConfig
from shared.llm_client import api_connection
from world_module.registry import city_version_context
from app.assets.benchmark_layout import iter_batches, task_dirs
from test_runner.verification.shared.formalization import is_formal_task
from test_runner.runner import run_all_tasks as _test_runner_run_all
from test_runner.place.runner import run_place_manifest
from vis.tasks.server import ROOT as PIPELINE_ROOT, send

_RUNS_CACHE: dict[str, tuple[tuple, dict]] = {}
_RUNS_CACHE_LOCK = threading.RLock()
_MODEL_LABELS = {'cliproxy_gpt55_pro': 'GPT-5.5 (Pro)', 'qwen_vl': 'Qwen-VL-Max', 'qwen38_max': 'Qwen3.8-Max', 'deepseek_v3': 'DeepSeek-V3'}
_TEST_RUNS: dict[str, dict] = {}
_TEST_RUN_LOCK = threading.RLock()
_ACTIVE_TEST_RUN_STATUSES = frozenset({'queued', 'running', 'cancelling'})

def _managed_resume_worker_alive(state: dict) -> bool:
    from test_runner.resume import managed_worker_alive
    return managed_worker_alive(state)

def _run_state_path(run_dir: Path) -> Path:
    return run_dir / 'run_state.json'

def _persist_run_state(state: dict) -> None:
    path = Path(state['output_dir']) / 'run_state.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {key: value for (key, value) in state.items() if not key.startswith('_')})

def _run_state_snapshot(state: dict) -> dict:
    return {key: value for (key, value) in state.items() if not key.startswith('_')}

def _normalize_run_result(item: dict) -> dict:
    """Read standard, place, and historical result rows through one UI contract."""
    passed = item.get('passed')
    total = item.get('total')
    if passed is None:
        passed = item.get('success_count', item.get('hard_passed', 1 if item.get('success') else 0))
    if total is None:
        total = item.get('hard_total', item.get('key_node_count', 1 if 'success' in item else 0))
    passed = int(passed or 0)
    total = int(total or 0)
    success = item.get('success')
    grade = item.get('grade') or ('PASS' if success is True else 'FAIL' if success is False else 'PASS' if total and passed >= total else 'FAIL')
    return {'task_id': str(item.get('task_id', '')), 'task_folder': str(item.get('task_folder', item.get('task_id', ''))), 'task_type': str(item.get('task_type', 'unknown')), 'grade': grade, 'passed': passed, 'total': total, 'score': item.get('score', item.get('total_score', 0)), 'duration_s': item.get('duration_s', 0), 'token_usage': item.get('tokens_used', item.get('token_usage', {})) or {}, 'raw': item}

def _run_results(run_dir: Path) -> list[dict]:
    """Read persisted result rows without making UI callers infer file formats."""
    rows: list[dict] = []
    results_path = run_dir / 'results.jsonl'
    if results_path.is_file():
        for line in results_path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                rows.append(json.loads(line))
    if not rows:
        summary_path = run_dir / 'results.json'
        if summary_path.is_file():
            rows = list(read_json(summary_path).get('tasks') or [])
    return rows

def _run_view(run_dir: Path) -> dict:
    from .run_cache import cached_view
    return cached_view(run_dir, PIPELINE_ROOT / 'outputs' / 'cache' / 'task_browser' / 'runs', _build_run_view)

def _run_summary(run_dir: Path) -> dict:
    return copy.deepcopy(_run_view(run_dir)['summary'])

def _build_run_view(run_dir: Path) -> dict:
    """Expose one stable summary for live, completed, and legacy evaluations."""
    state_path = _run_state_path(run_dir)
    manifest_path = run_dir / 'run_manifest.json'
    results_path = run_dir / 'results.json'
    state = read_json(state_path) if state_path.is_file() else {}
    manifest = read_json(manifest_path) if manifest_path.is_file() else {}
    completed = read_json(results_path) if results_path.is_file() else {}
    rows = _run_results(run_dir) if (run_dir / 'results.jsonl').is_file() else list(completed.get('tasks') or [])
    normalized_rows = [_normalize_run_result(row) for row in rows]
    for row in normalized_rows:
        row['raw'] = {key: row['raw'][key] for key in ('source_tasks_dir', 'source_batch_id', 'source_batch', 'infrastructure_failure') if key in row['raw']}
    scope = state.get('scope') or manifest.get('scope') or completed.get('scope') or {}
    task_count = int(completed.get('task_count') or state.get('total_count') or completed.get('total_count') or len(normalized_rows))
    completed_count = int(state.get('completed_count') or len(normalized_rows))
    passed = int(completed.get('passed') if completed.get('passed') is not None else state.get('passed') if state.get('passed') is not None else sum((1 for row in normalized_rows if row.get('grade') == 'PASS')))
    failed = int(completed.get('failed') if completed.get('failed') is not None else state.get('failed') if state.get('failed') is not None else max(0, completed_count - passed))
    token_usage = completed.get('token_usage') or state.get('token_usage') or completed.get('tokens_used') or {}
    tasks_by_type: dict[str, dict] = {}
    for row in normalized_rows:
        task_type = row.get('task_type') or 'unknown'
        entry = tasks_by_type.setdefault(task_type, {'task_count': 0, 'passed': 0, 'token_usage': {'total': 0}})
        entry['task_count'] += 1
        entry['passed'] += 1 if row.get('grade') == 'PASS' else 0
        entry['token_usage']['total'] += int((row.get('token_usage') or {}).get('total', 0) or 0)
    summary = {'run_id': str(state.get('run_id') or manifest.get('run_id') or run_dir.name), 'status': state.get('status') or completed.get('status') or 'completed', 'phase': state.get('phase') or completed.get('phase') or 'completed', 'task_count': task_count, 'completed_count': completed_count, 'passed': passed, 'failed': failed, 'token_usage': token_usage, 'elapsed_seconds': state.get('elapsed_seconds') or completed.get('elapsed_seconds') or 0, 'started_at': state.get('started_at') or manifest.get('created_at') or '', 'finished_at': state.get('finished_at') or completed.get('finished_at'), 'error': state.get('error') or completed.get('error'), 'scope': scope, 'model_key': manifest.get('model_key') or state.get('model_key') or completed.get('model_key') or '', 'dynamic_event_mode': manifest.get('dynamic_event_mode') or state.get('dynamic_event_mode') or completed.get('dynamic_event_mode') or 'shared_world', 'selected_tasks': scope.get('selected_tasks') or manifest.get('selected_tasks') or [], 'operation_ledger': state.get('operation_ledger') or completed.get('operation_ledger') or {}, 'tasks_by_type': tasks_by_type}
    return {'summary': summary, 'results': normalized_rows}

def _run_worker(state: dict, *, tasks_dirs: list[Path], selected_task_folders: list[Path], route_cache: Path | None, model_key: str, config_yaml: str, world: str, approved_only: bool, task_type_filter: str) -> None:
    """Run a test in the background and persist every observable transition."""
    run_dir = Path(state['output_dir'])
    start = time.monotonic()

    def on_progress(update: dict) -> None:
        with _TEST_RUN_LOCK:
            state.update(update)
            state['status'] = 'running' if update.get('phase') != 'completed' else 'completed'
            state['elapsed_seconds'] = round(time.monotonic() - start, 3)
            state['updated_at'] = datetime.now().isoformat(timespec='seconds')
            _persist_run_state(state)
    try:
        (results, summary) = _test_runner_run_all(tasks_dirs, route_cache_path=str(route_cache) if route_cache else '', model_key=model_key, config_yaml=config_yaml, output_path=str(run_dir / 'results.json'), world_path=world, approved_only=approved_only, task_type_filter=task_type_filter, progress_callback=on_progress, selected_task_folders=selected_task_folders, dynamic_event_mode=state.get('dynamic_event_mode', 'shared_world'))
        with _TEST_RUN_LOCK:
            state.update({'status': 'completed', 'phase': 'completed', 'completed_count': summary.task_count, 'total_count': summary.task_count, 'passed': summary.passed, 'failed': summary.task_count - summary.passed, 'token_usage': summary.token_usage, 'elapsed_seconds': round(time.monotonic() - start, 3), 'finished_at': summary.finished_at, 'updated_at': datetime.now().isoformat(timespec='seconds')})
            _persist_run_state(state)
    except Exception as exc:
        with _TEST_RUN_LOCK:
            state.update({'status': 'failed', 'phase': 'failed', 'error': str(exc), 'elapsed_seconds': round(time.monotonic() - start, 3), 'finished_at': datetime.now().isoformat(), 'updated_at': datetime.now().isoformat(timespec='seconds')})
            _persist_run_state(state)

def _model_options(config_yaml: str='config.yaml') -> dict[str, list[dict[str, str]]]:
    """Build public, credential-free model choices from the pipeline config."""
    if not Path(config_yaml).is_file():
        return {'generation': [], 'testing': []}
    config = PipelineConfig(config_yaml)
    endpoints = config.get('model_endpoints', {}) or {}
    generation = [{'endpoint_key': endpoint_key, 'label': _MODEL_LABELS.get(endpoint_key, endpoint.get('model', endpoint_key))} for (endpoint_key, endpoint) in endpoints.items() if endpoint.get('task_generation', False)]
    testing = [{'endpoint_key': endpoint_key, 'label': _MODEL_LABELS.get(endpoint_key, endpoint.get('model', endpoint_key))} for (endpoint_key, endpoint) in endpoints.items() if endpoint.get('testing', endpoint.get('supports_vision', False))]
    return {'generation': generation, 'testing': testing}

def _external_failure_source(raw: str) -> str:
    """Infer the upstream service from a sanitized child-process failure."""
    text = raw.lower()
    if 'dashscope.aliyuncs.com' in text or 'qwen-' in text:
        return 'Qwen / DashScope'
    if 'maps.googleapis.com' in text or 'places.googleapis.com' in text or 'routes.googleapis.com' in text:
        return 'Google Maps / Street View'
    if 'yunwu.ai' in text or 'yunwu' in text:
        return 'Yunwu'
    if 'api.openai.com' in text or 'openai' in text:
        return 'OpenAI'
    return '构建流程中的外部服务'

def _describe_external_failure(raw: str, model_label: str) -> dict[str, str]:
    """Translate common provider failures into an actionable, safe UI message."""
    text = raw.lower()
    if 'formal task validation incomplete' in text:
        return {'code': 'task_construction_incomplete', 'message': '构建未产生足够的有效任务。请查看候选构建记录中的具体原因；这不是外部服务调用失败。'}
    if 'character has no public real-place starting anchor' in text:
        return {'code': 'missing_public_starting_place', 'message': '抽中的人物只有坐标，缺少可公开的真实出发地点名称，任务尚未生成。这不是模型或地图服务故障，需要先确定本次任务的公开起点。'}
    if any((marker in text for marker in ('winerror', 'filenotfounderror', 'permissionerror', 'os.replace'))):
        return {'code': 'local_artifact_write_failed', 'message': '任务文件写入失败，候选任务未登记为正式批次。请检查任务目录命名或本地文件权限后新建批次重试。'}
    source = _external_failure_source(raw)
    prefix = f'所选模型“{model_label}”' if source == 'Qwen / DashScope' else source
    if 'invalid_api_key' in text or 'incorrect api key' in text:
        return {'code': 'invalid_api_key', 'message': f'{prefix}的 API key 无效、已撤销或与当前项目不匹配。请在 config.yaml 更新该 endpoint 的凭据。'}
    if ' 401' in text or 'status=401' in text:
        return {'code': 'authentication_failed', 'message': f'{prefix}认证被拒绝（401）。请检查该 endpoint 的 API key 与项目权限。'}
    if ' 403' in text or 'status=403' in text:
        return {'code': 'permission_denied', 'message': f'{prefix}没有当前操作所需权限（403）。请检查项目、组织或模型访问权限。'}
    if ' 429' in text or 'rate limit' in text or 'rate_limit' in text:
        return {'code': 'rate_limited', 'message': f'{prefix}触发了配额或限流（429）。请稍后重试，或降低批量与并发。'}
    if 'model_not_found' in text or 'does not exist' in text:
        return {'code': 'model_not_found', 'message': f'{prefix}的模型标识不可用。请检查 config.yaml 中该 endpoint 的 model 字段。'}
    if 'timed out' in text or 'timeout' in text:
        return {'code': 'upstream_timeout', 'message': f'{prefix}请求超时。请检查网络、服务商状态或稍后重试。'}
    if 'connection' in text or 'name resolution' in text or 'proxy' in text:
        return {'code': 'network_error', 'message': f'{prefix}网络连接失败。请检查该服务的网络、代理与 endpoint 地址。'}
    return {'code': 'generation_failed', 'message': f'{prefix}调用失败。服务端已保留脱敏诊断，请检查生成服务日志。'}

def _preflight_test_model(model_key: str, config_yaml: str) -> dict[str, str] | None:
    """Fail fast for authentication and quota errors before a test batch starts."""
    config = PipelineConfig(config_yaml)
    endpoint = config.model_endpoint(model_key)
    base_url = str(endpoint.get('base_url', '')).rstrip('/')
    parsed_url = urlparse(base_url)
    if parsed_url.scheme not in {'http', 'https'} or not parsed_url.netloc:
        return None
    try:
        (conn, path) = api_connection(base_url, 'models', timeout=20)
        conn.request('GET', path, headers={'Authorization': f"Bearer {endpoint.get('api_key', '')}"})
        response = conn.getresponse()
        body = response.read().decode('utf-8', 'replace')
        conn.close()
    except OSError:
        return None
    if response.status not in {401, 403, 429}:
        return None
    diagnostic = _describe_external_failure(body + f' status={response.status}', _MODEL_LABELS.get(model_key, model_key))
    diagnostic['status'] = str(response.status)
    return diagnostic

def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False

def _batch_metadata(batch_path: Path) -> dict:
    """Read optional presentation metadata without making it authoritative."""
    for name in ('batch_metadata.json', 'world_provenance.json'):
        path = batch_path / name
        if not path.is_file():
            continue
        try:
            payload = read_json(path)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {}

def _batch_sort_key(batch_path: Path) -> tuple[str, str]:
    """Choose newest by recorded creation time, never by arbitrary folder text."""
    metadata = _batch_metadata(batch_path)
    created_at = str(metadata.get('created_at') or '')
    if not created_at:
        created_at = datetime.fromtimestamp(batch_path.stat().st_mtime).isoformat()
    return (created_at, batch_path.name)

def _select_batches(benchmarks_root: str | Path, *, task_type: str='', city_id: str='', world_id: str, target_batch: str='latest', include_history: bool=False) -> list[Path]:
    """Resolve one current batch per task-type/city scope for a managed world."""
    root = Path(benchmarks_root)
    selected: dict[tuple[str, str], Path] = {}
    for candidate in iter_batches(root, task_type=task_type, world_id=world_id, city_scope=city_id):
        if not include_history and (not _batch_version_compatibility(candidate, world_id).get('compatible')):
            continue
        key = (candidate.parents[2].name, candidate.parent.name)
        if target_batch != 'latest':
            if candidate.name == target_batch:
                selected[key] = candidate
        elif key not in selected or _batch_sort_key(candidate) > _batch_sort_key(selected[key]):
            selected[key] = candidate
    return [selected[key] for key in sorted(selected)]

def _batch_version_compatibility(batch_dir: Path, world_id: str) -> dict:
    """Check a batch sidecar against the World Studio's active city versions."""
    provenance_path = batch_dir / 'world_provenance.json'
    if not provenance_path.is_file():
        return {'compatible': False, 'reason': 'The batch is missing world_provenance.json; its city version cannot be verified.'}
    try:
        provenance = read_json(provenance_path)
        context = city_version_context(world_id)
    except Exception as exc:
        return {'compatible': False, 'reason': f'Cannot read the version context: {exc}'}
    if provenance.get('world_id') != world_id:
        return {'compatible': False, 'reason': f"The task belongs to {provenance.get('world_id') or 'unbound world'}; the current world is {world_id}."}
    requirements = provenance.get('required_city_versions') or {}
    if not requirements:
        return {'compatible': False, 'reason': 'The batch is missing required city versions; reproducibility cannot be verified.'}
    active = context.get('active_city_versions') or {}
    unmet = [f'{city_id} requires v{required}; the active version is v{active.get(city_id, 0)}' for (city_id, required) in requirements.items() if int(active.get(city_id, 0) or 0) < int(required or 0)]
    if unmet:
        return {'compatible': False, 'reason': '；'.join(unmet), 'required_city_versions': requirements, 'active_city_versions': active}
    return {'compatible': True, 'required_city_versions': requirements, 'active_city_versions': active}

def _resolve_run_tasks_dirs(benchmarks_root: str, *, task_type: str, city: str, batch: str, world_id: str) -> list[Path]:
    """Resolve a current composed benchmark or one explicit type-history batch."""
    root = Path(benchmarks_root).resolve()
    if not root.is_dir() or not world_id:
        return []
    if batch != 'latest' and (not task_type or not city):
        return []
    selected = _select_batches(root, task_type=task_type, city_id=city, world_id=world_id, target_batch=batch or 'latest')
    return [candidate / 'tasks' for candidate in selected if (candidate / 'tasks').is_dir()]

def _select_formal_run_tasks(source_tasks_dirs: list[Path], *, task_limit: int=0, approved_only: bool=False) -> list[Path]:
    """Choose a stable eligible evaluation sample from resolved batches."""
    candidates: list[tuple[tuple[str, str, str], Path]] = []
    for tasks_root in source_tasks_dirs:
        batch = tasks_root.parent
        for task_dir in task_dirs(batch):
            if not is_formal_task(task_dir, batch.parents[2].name):
                continue
            if approved_only:
                if not _is_approved_task(task_dir):
                    continue
            try:
                task_id = str(read_json(task_dir / 'task.json').get('task_id') or task_dir.name)
            except Exception:
                task_id = task_dir.name
            candidates.append(((str(batch.resolve()), task_id, task_dir.name), task_dir))
    candidates.sort(key=lambda item: item[0])
    paths = [path for (_, path) in candidates]
    return paths[:task_limit] if task_limit else paths

def _selected_task_record(task_dir: Path) -> dict:
    """Persist exact task selection so a sampled evaluation is reproducible."""
    task = read_json(task_dir / 'task.json')
    return {'task_id': str(task.get('task_id') or task_dir.name), 'task_type': str(task.get('task_type') or ''), 'task_folder': str(task_dir), 'source_batch': task_dir.parent.parent.name}

def _is_approved_task(task_dir: Path) -> bool:
    """Read one batch-level review decision without granting implicit approval."""
    review_path = task_dir.parent.parent / 'review_status.json'
    if not review_path.is_file():
        return False
    try:
        return (read_json(review_path).get(task_dir.name) or {}).get('status') == 'approved'
    except Exception:
        return False

def _approved_task_count(tasks_dir: Path) -> int:
    """Count approved canonical tasks; a missing review file means zero approved."""
    review_path = tasks_dir.parent / 'review_status.json'
    if not review_path.is_file():
        return 0
    try:
        review_data = read_json(review_path)
    except Exception:
        return 0
    return sum((1 for task_dir in tasks_dir.iterdir() if task_dir.is_dir() and is_formal_task(task_dir) and ((review_data.get(task_dir.name) or {}).get('status') == 'approved')))

def _approved_task_count_many(tasks_dirs: list[Path]) -> int:
    return sum((_approved_task_count(tasks_dir) for tasks_dir in tasks_dirs))

class EvaluationHTTP:
    runs_root_override = ''

    def __init__(self, handler, benchmarks_root, params):
        self.handler = handler
        self.benchmarks_root = str(benchmarks_root)
        self.params = params
        self.manifest_path = ''

    def _json(self, value):
        send(self.handler, value)

    def _error(self, message, status):
        send(self.handler, {'error': str(message)}, status)

    def _read_json_body(self):
        try:
            length = int(self.handler.headers.get('Content-Length', '0'))
            value = json.loads(self.handler.rfile.read(length).decode('utf-8'))
            if not isinstance(value, dict):
                raise ValueError('Expected a JSON object.')
            run_id = value.get('run_id')
            if run_id is not None and (isinstance(run_id, str) and ':' in run_id or not isinstance(run_id, str) or not run_id or run_id in {'.', '..'} or ('/' in run_id) or ('\\' in run_id)):
                raise ValueError('Invalid run identifier.')
            return value
        except (ValueError, UnicodeError) as exc:
            self._error(str(exc), 400)
            return None

    def _current_browsing_world_id(self):
        return self.params.get('world') or self.handler.active_world_id

    def _resolve_world_path(self):
        return getattr(self.handler, 'world_path', None)

    def _is_place_manifest(self):
        return False

    def handle(self, method, path):
        run = self.params.get('run', '')
        if path in {'/api/run', '/api/run/status', '/api/run/task'} and not run:
            self._error('Missing run parameter.', 400)
            return True
        if path == '/api/run/task' and not self.params.get('task_id'):
            self._error('Missing task identifier.', 400)
            return True
        if path == '/api/runs' and method == 'GET':
            self._serve_runs()
        elif path == '/api/run/start' and method == 'POST':
            self._start_run()
        elif path == '/api/run' and method == 'GET':
            self._serve_run(run)
        elif path == '/api/run' and method == 'DELETE':
            self._delete_run(run)
        elif path == '/api/run/status' and method == 'GET':
            self._serve_run_status(run)
        elif path == '/api/run/task' and method == 'GET':
            self._serve_run_task(run, self.params.get('task_id', ''))
        else:
            return False
        return True

    def _start_run(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        mode = payload.get('mode', 'llm')
        run_id = payload.get('run_id') or f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        world = payload.get('world') or str(self._resolve_world_path() or '')
        batch = payload.get('batch', '')
        task_type = payload.get('task_type', '')
        city = payload.get('city', '')
        task_type_filter = payload.get('task_type_filter', '')
        approved_only = payload.get('approved_only', False)
        try:
            task_limit = int(payload.get('task_limit') or 0)
        except (TypeError, ValueError):
            self._error('task_limit must be a non-negative integer', 400)
            return
        if task_limit < 0:
            self._error('task_limit must be a non-negative integer', 400)
            return
        active_world_id = self._current_browsing_world_id()
        if self._is_place_manifest() and mode in {'oracle', 'judge-only'}:
            if run_place_manifest is None:
                self._error('Place test_runner is not available in this environment.', 501)
                return
            result = run_place_manifest(self.manifest_path, world, output, mode=mode)
            self._json({'ok': True, 'run_id': run_id, 'output': str(output), **result})
            return
        tasks_dirs = _resolve_run_tasks_dirs(self.benchmarks_root, task_type=task_type, city=city, batch=batch or 'latest', world_id=active_world_id)
        if not tasks_dirs:
            self._error('Cannot resolve the selected benchmark scope. Select a current world, or choose one task type and city before opening its history.', 400)
            return
        batch_dirs = [tasks_dir.parent for tasks_dir in tasks_dirs]
        version_checks = [_batch_version_compatibility(batch_dir, active_world_id) for batch_dir in batch_dirs]
        incompatible = [check for check in version_checks if not check.get('compatible')]
        if incompatible:
            self._error('The selected batch is incompatible with the current world version: ' + incompatible[0].get('reason', 'Insufficient version information'), 409)
            return
        composed = len(batch_dirs) > 1 or not task_type or (not city)
        output_kind = 'benchmark' if composed else task_type
        output_city = city or 'all_cities'
        output_batch = 'current' if composed else batch_dirs[0].name
        output = self._runs_root() / output_kind / active_world_id / output_city / output_batch / run_id
        if _test_runner_run_all is None:
            self._error('test_runner is not available in this environment.', 501)
            return
        route_cache = None
        model_key = payload.get('model_key') or 'qwen_vl'
        dynamic_event_mode = payload.get('dynamic_event_mode', 'shared_world')
        if dynamic_event_mode not in {'shared_world', 'task_only'}:
            self._error('Unsupported dynamic_event_mode', 400)
            return
        config_yaml = payload.get('config_yaml', 'config.yaml')
        allowed_models = {item['endpoint_key'] for item in _model_options(config_yaml)['testing']}
        if model_key not in allowed_models:
            self._error('Unsupported test model selection', 400)
            return
        if approved_only and _approved_task_count_many(tasks_dirs) == 0:
            self._error('No approved tasks exist in the selected test scope.', 409)
            return
        selected_task_folders = _select_formal_run_tasks(tasks_dirs, task_limit=task_limit, approved_only=bool(approved_only))
        if not selected_task_folders:
            self._error('No formal tasks are available in the selected test scope.', 409)
            return
        preflight = _preflight_test_model(model_key, config_yaml)
        if preflight:
            self._error(preflight['message'], int(preflight['status']))
            return
        if output.exists():
            self._error(f'Run already exists: {run_id}', 409)
            return
        selected_count = len(selected_task_folders)
        selected_tasks = [_selected_task_record(path) for path in selected_task_folders]
        source_batches = [{'task_type': batch_dir.parents[2].name, 'city_id': batch_dir.parent.name, 'batch_id': batch_dir.name, 'tasks_dir': str(batch_dir / 'tasks'), 'world_provenance': str(batch_dir / 'world_provenance.json')} for batch_dir in batch_dirs]
        state = {'schema_version': 'geoword.test_run_state.v1', 'run_id': run_id, 'dynamic_event_mode': dynamic_event_mode, 'status': 'queued', 'phase': 'queued', 'output_dir': str(output), 'started_at': datetime.now().isoformat(timespec='seconds'), 'updated_at': datetime.now().isoformat(timespec='seconds'), 'finished_at': None, 'elapsed_seconds': 0.0, 'completed_count': 0, 'total_count': selected_count, 'current_task_id': '', 'passed': 0, 'failed': 0, 'token_usage': {'prompt': 0, 'completion': 0, 'total': 0}, 'error': None, 'scope': {'kind': 'current_benchmark' if composed else 'task_batch', 'task_type': task_type or 'all', 'city': city or 'all_cities', 'batch': batch or 'latest', 'approved_only': bool(approved_only), 'task_type_filter': task_type_filter, 'world_id': active_world_id, 'source_batches': source_batches, 'task_limit': task_limit, 'selected_tasks': selected_tasks}, 'operation_ledger': {'cost_status': 'not_configured', 'money_estimate': None, 'note': 'Tokens and elapsed time are measured; no price table is configured, so no monetary cost is fabricated.'}}
        output.mkdir(parents=True, exist_ok=False)
        (output / 'tasks').mkdir(parents=False, exist_ok=False)
        write_json(output / 'run_manifest.json', {'schema_version': 'geoword.test_run_manifest.v1', 'run_id': run_id, 'dynamic_event_mode': dynamic_event_mode, 'created_at': state['started_at'], 'tasks_dirs': [str(path) for path in tasks_dirs], 'world_path': world, 'route_cache_path': '', 'route_cache_policy': 'per_task_source_batch', 'model_key': model_key, 'config_yaml': config_yaml, 'scope': state['scope'], 'task_limit': task_limit, 'selected_tasks': selected_tasks, 'version_checks': version_checks, 'note': 'Execution facts are appended to run_state.json and result artifacts; no historical artifacts were mutated.'})
        with _TEST_RUN_LOCK:
            _TEST_RUNS[run_id] = state
            _persist_run_state(state)
        threading.Thread(target=_run_worker, kwargs={'state': state, 'tasks_dirs': tasks_dirs, 'selected_task_folders': selected_task_folders, 'route_cache': route_cache, 'model_key': model_key, 'config_yaml': config_yaml, 'world': world, 'approved_only': bool(approved_only), 'task_type_filter': task_type_filter}, daemon=True).start()
        self._json({'ok': True, 'run_id': run_id, 'output': str(output), 'status': 'queued', 'task_count': selected_count, 'task_type': task_type, 'city': city, 'batch': batch or 'latest', 'approved_only': bool(approved_only), 'task_type_filter': task_type_filter, 'task_limit': task_limit, 'selected_task_ids': [item['task_id'] for item in selected_tasks]})

    def _serve_runs(self) -> None:
        cache_key = str(self._runs_root())
        with _RUNS_CACHE_LOCK:
            from .run_cache import revision
            root = self._runs_root()
            run_dirs = {path.parent for name in ('run_state.json', 'run_manifest.json', 'results.json', 'run_summary.json') for path in root.rglob(name)} if root.exists() else set()
            current_revision = tuple(((str(path), revision(path)) for path in sorted(run_dirs)))
            cached = _RUNS_CACHE.get(cache_key)
            if cached and current_revision == cached[0]:
                self._json(copy.deepcopy(cached[1]))
                return
            runs = []
            if root.exists():
                for run_dir in run_dirs:
                    item = _run_summary(run_dir)
                    item['path'] = str(run_dir)
                    runs.append(item)
            runs.sort(key=lambda item: (item.get('started_at') or '', item.get('run_id') or ''), reverse=True)
            payload = {'runs': runs}
            _RUNS_CACHE[cache_key] = (current_revision, copy.deepcopy(payload))
        self._json(payload)

    def _serve_run_status(self, run_id: str) -> None:
        run_dir = self._resolve_run_dir(run_id)
        if not run_dir:
            self._error(f'Run not found: {run_id}', 404)
            return
        state_path = _run_state_path(run_dir)
        if not state_path.is_file():
            self._json({'run_id': run_id, 'status': 'completed', 'phase': 'legacy', 'completed_count': 0, 'total_count': 0, 'note': 'Legacy run has no live state artifact.'})
            return
        state = read_json(state_path)
        if state.get('status') in {'queued', 'running'} and run_id not in _TEST_RUNS and (not _managed_resume_worker_alive(state)):
            state.update({'status': 'interrupted', 'phase': 'interrupted', 'error': 'The browser server restarted before this in-memory worker completed.', 'updated_at': datetime.now().isoformat(timespec='seconds')})
            _persist_run_state(state)
        self._json(_run_state_snapshot(state))

    def _serve_run(self, run_id: str) -> None:
        run_dir = self._resolve_run_dir(run_id)
        if not run_dir:
            self._error(f'Run not found: {run_id}', 404)
            return
        view = _run_view(run_dir)
        summary = view['summary']
        normalized = view['results']
        self._json({'run_id': run_id, 'summary': summary, 'results': normalized, 'audit': {'run_id': summary['run_id'], 'selected_tasks': summary['selected_tasks'], 'scope': summary['scope']}})

    def _serve_run_task(self, run_id: str, task_id: str) -> None:
        run_dir = self._resolve_run_dir(run_id)
        if not run_dir:
            self._error(f'Run not found: {run_id}', 404)
            return
        task_path = self._resolve_run_task_dir(run_dir, task_id)
        if not _is_relative_to(task_path.resolve(), run_dir.resolve()):
            self._error('Task result is outside this run.', 400)
            return
        if not task_path.exists():
            self._error(f'Run task not found: {task_id}', 404)
            return
        if task_path.is_file() and task_path.suffix == '.json':
            flat = read_json(task_path)
            result = {'result_kind': 'agent_run', 'run_report': flat, 'trajectory': flat.get('trace', []), 'outcome': {'grade': flat.get('grade', 'UNKNOWN'), 'passed': flat.get('passed', 0), 'total': flat.get('total', 0), 'metrics': flat.get('metrics', []), 'token_usage': flat.get('tokens_used', {}), 'duration_s': flat.get('duration_s', 0), 'final_answer': flat.get('final_answer')}}
        else:
            result = {}
            for fname in ['run_report.json', 'trajectory.json', 'agent_trace_graph.json', 'matched_key_operation_graph.json', 'judge_report.json', 'llm_trace.json']:
                fp = task_path / fname
                if fp.exists():
                    result[fname.replace('.json', '')] = read_json(fp)
            result['result_kind'] = 'native_graph' if result.get('judge_report') else 'agent_run'
        self._json(result)

    def _delete_run(self, run_id: str) -> None:
        import shutil
        run_dir = self._resolve_run_dir(run_id)
        if not run_dir or not run_dir.exists():
            self._error(f'Run not found: {run_id}', 404)
            return
        if _run_summary(run_dir).get('status') in _ACTIVE_TEST_RUN_STATUSES:
            self._error('A running evaluation cannot be deleted.', 409)
            return
        shutil.rmtree(run_dir)
        self._json({'ok': True, 'deleted': run_id})

    def _runs_root(self) -> Path:
        if EvaluationHTTP.runs_root_override:
            return Path(EvaluationHTTP.runs_root_override)
        return PIPELINE_ROOT / 'outputs' / 'evaluations'

    def _resolve_run_dir(self, run_id: str) -> Path | None:
        raw = Path(run_id)
        candidate = raw if raw.is_absolute() else self._runs_root() / run_id
        resolved = candidate.resolve()
        if resolved.exists() and _is_relative_to(resolved, self._runs_root().resolve()):
            return resolved
        if not raw.is_absolute() and self._runs_root().is_dir():
            matches = [path for path in self._runs_root().rglob(run_id) if path.is_dir()]
            if len(matches) == 1:
                return matches[0].resolve()
        return None

    def _resolve_run_task_dir(self, run_dir: Path, task_id: str) -> Path:
        direct = run_dir / task_id
        if direct.exists():
            return direct
        flat = run_dir / 'tasks' / f'{task_id}.json'
        if flat.exists():
            return flat
        task_results_dir = run_dir / 'tasks'
        index_path = task_results_dir / 'task_index.json'
        if index_path.is_file():
            try:
                entries = read_json(index_path).get('entries') or []
                for entry in entries:
                    if entry.get('task_id') != task_id:
                        continue
                    candidate = task_results_dir / str(entry.get('file_name') or '')
                    if candidate.is_file():
                        return candidate
            except Exception:
                pass
        if task_results_dir.is_dir():
            for candidate in task_results_dir.glob('*.json'):
                if candidate.name == 'task_index.json':
                    continue
                try:
                    if read_json(candidate).get('task_id') == task_id:
                        return candidate
                except Exception:
                    continue
        for child in run_dir.iterdir():
            report = child / 'run_report.json'
            if child.is_dir() and report.exists():
                try:
                    if read_json(report).get('task_id') == task_id:
                        return child
                except Exception:
                    continue
        return direct
