"""Existing background generation lifecycle, enabled for Grounding only."""
from __future__ import annotations
import copy,hashlib,json,math,os,queue,re,secrets,shutil,socket,subprocess,sys,threading,time,uuid
from typing import Any
from world_module.registry import city_version_context
from task_module._shared.task_contract import validate_task_folder
from vis.tasks.browse_validation import recorded_formal_task
from datetime import datetime
from pathlib import Path
from shared.io import read_json,write_json
from shared.config_loader import PipelineConfig
from shared.llm_client import api_connection
from task_module._shared.benchmark_layout import batch_dir as managed_batch_dir,iter_batches,task_dirs
from task_module._shared.formalization import is_formal_task
from task_module.task_schema import TASK_TYPES
from vis.tasks.server import ROOT as PIPELINE_ROOT,send
from vis.tasks.workbench_http import WorkbenchHTTP
from vis.tasks.evaluation_http import _model_options
WORKSPACE_ROOT=PIPELINE_ROOT
# Existing route-cost display constant; Grounding itself makes no route calls.
UNIT_PRICE_USD=0.005
_MIXED_FEASIBILITY_TASK_TYPES = frozenset()
_GENERATION_ACTIVE_STATES = {'queued', 'running', 'cancelling'}
_GENERATION_JOBS: dict[str, dict] = {}
_GENERATION_PROCESSES: dict[str, subprocess.Popen] = {}
_GENERATION_JOB_LOCK = threading.RLock()

def _generation_target_count(batch_dir: Path, task_type: str, mode: str, requested_count: int) -> int:
    """Translate the UI's append amount into the generator's batch-total target."""
    if mode != 'append':
        return int(requested_count)
    existing_formal = 0
    for task_dir in task_dirs(batch_dir):
        report_path = task_dir / 'task_validation.json'
        if not report_path.is_file():
            continue
        try:
            report = read_json(report_path)
        except Exception:
            continue
        if report.get('valid') and report.get('state') == 'formal':
            existing_formal += 1
    return existing_formal + int(requested_count)

def _model_label(options: list[dict[str, str]], key: str, field: str) -> str:
    for item in options:
        if item.get(field) == key:
            return item['label']
    return key

def _sanitize_external_error(text: str) -> str:
    """Remove credentials before an upstream failure reaches the browser."""
    return re.sub('sk-[A-Za-z0-9_-]+', '[REDACTED]', text)

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

def _generation_subprocess_env(overrides: dict[str, str] | None=None) -> dict[str, str]:
    """Give browser-triggered builds the same network environment as the UI."""
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    env.update(overrides or {})
    return env

def _generation_jobs_root() -> Path:
    return PIPELINE_ROOT / 'outputs' / 'logs' / 'vis' / 'jobs'

def _route_cost_actual(path: str | Path | None, run_id: str='') -> dict[str, Any]:
    current = {'api_requests': 0, 'coordinate_reuse': 0, 'cost_usd': 0.0}
    cumulative = {'api_requests': 0, 'coordinate_reuse': 0, 'cost_usd': 0.0}
    event_path = Path(path) if path else None
    baseline_path = event_path.parent / 'route_cost_baseline.json' if event_path else None
    baseline = read_json(baseline_path) if baseline_path and baseline_path.is_file() else {}
    cumulative['api_requests'] = int(baseline.get('historical_api_requests') or 0)
    cumulative['coordinate_reuse'] = int(baseline.get('historical_coordinate_reuse') or 0)
    if event_path and event_path.is_file():
        try:
            with event_path.open('r', encoding='utf-8') as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    event_type = str(event.get('event_type') or '')
                    field = 'api_requests' if event_type == 'api_request' else 'coordinate_reuse' if event_type == 'coordinate_reuse' else ''
                    if not field:
                        continue
                    cumulative[field] += 1
                    if run_id and str(event.get('run_id') or '') == run_id:
                        current[field] += 1
        except OSError:
            pass
    current['cost_usd'] = round(current['api_requests'] * UNIT_PRICE_USD, 2)
    cumulative['cost_usd'] = round(cumulative['api_requests'] * UNIT_PRICE_USD, 2)
    return {'current': current, 'cumulative': cumulative, 'unit_price_usd': UNIT_PRICE_USD, 'tracking_scope': 'current_world_city_active_version', 'tracking_note': '含历史缓存兼容基线；启用后按真实事件累计' if baseline else '自真实成本统计启用后累计', 'historical_baseline': bool(baseline)}

def _generation_snapshot(job: dict) -> dict:
    """Return the browser-safe state persisted for a generation job."""
    started = float(job.get('_started_monotonic', time.monotonic()))
    return {key: value for (key, value) in {'job_id': job['job_id'], 'status': job['status'], 'phase': job['phase'], 'phase_label': job['phase_label'], 'detail': job.get('detail', ''), 'task_type': job['task_type'], 'city': job['city'], 'batch': job['batch'], 'mode': job['mode'], 'model_label': job['model_label'], 'requested_count': job['requested_count'], 'dynamic_pretask_admission': job.get('dynamic_pretask_admission'), 'dynamic_source_inventory': job.get('dynamic_source_inventory'), 'dynamic_configuration': job.get('dynamic_configuration'), 'dynamic_cost_preview': job.get('dynamic_cost_preview'), 'construction_count': job.get('construction_count', 0), 'candidate_count': job.get('candidate_count', 0), 'completed_count': job.get('completed_count', 0), 'observed_count': job.get('observed_count', 0), 'rejected_count': job.get('rejected_count', 0), 'generated_folders': job.get('generated_folders', []), 'started_at': job['started_at'], 'last_activity_at': job.get('last_activity_at', job['started_at']), 'elapsed_seconds': int(time.monotonic() - started), 'error': job.get('error', ''), 'diagnostic_log': job.get('diagnostic_log', ''), 'output_dir': job['output_dir'], 'route_cost_actual': _route_cost_actual(job.get('route_cost_events_path'), job.get('job_id', '')) if job.get('task_type') in {'route_planning', 'multi_entity', 'itinerary', 'region_aware', 'safety_planning', 'dynamic_replanning'} else None}.items()}

def _persist_generation_job(job: dict) -> None:
    """Atomically persist status so a browser refresh can recover the job."""
    path = Path(job['_state_path'])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix('.tmp')
    tmp_path.write_text(json.dumps(_generation_snapshot(job), ensure_ascii=False, indent=2), encoding='utf-8')
    tmp_path.replace(path)

def _update_generation_job(job: dict, **updates) -> None:
    with _GENERATION_JOB_LOCK:
        job.update(updates)
        job['last_activity_at'] = datetime.now().isoformat(timespec='seconds')
        _persist_generation_job(job)

def _active_generation_job() -> dict | None:
    with _GENERATION_JOB_LOCK:
        return next((job for job in _GENERATION_JOBS.values() if job.get('status') in _GENERATION_ACTIVE_STATES), None)

def _generation_task_progress(job: dict) -> tuple[list[str], list[str], int]:
    """Return observed, formalized, and rejected folders for this job only."""
    tasks_dir = Path(job['output_dir']) / 'tasks'
    if not tasks_dir.exists():
        return ([], [], 0)
    baseline = set(job.get('baseline_folders', []))
    observed: list[str] = []
    valid: list[str] = []
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir() or task_dir.name in baseline or (not (task_dir / 'task.json').exists()):
            continue
        observed.append(task_dir.name)
        stamp_path = task_dir / 'task_validation.json'
        if not stamp_path.is_file():
            continue
        try:
            stamp = read_json(stamp_path)
            report = validate_task_folder(task_dir, job['task_type'])
        except Exception:
            continue
        if stamp.get('state') == 'formal' and stamp.get('valid') and report.get('valid'):
            valid.append(str(task_dir.relative_to(PIPELINE_ROOT / 'outputs' / 'benchmarks')).replace('\\', '/'))
    rejected_root = Path(job['output_dir']) / 'candidates' / 'rejected'
    rejected = len([path for path in rejected_root.iterdir() if path.is_dir() and (path / 'task_validation.json').is_file()]) if rejected_root.is_dir() else 0
    return (observed, valid, rejected)

def _generation_preservation_detail(valid_folders: list[str]) -> str:
    """Describe durable formal output without claiming nonexistent work."""
    return '已保留已通过契约校验的任务' if valid_folders else '尚未写入通过契约校验的任务'

def _generation_phase_from_output(line: str) -> tuple[str, str, str] | None:
    """Map stable generator progress lines to a user-visible build phase."""
    if 'Phase 1: EventFactory' in line or '[EnvEvents]' in line:
        return ('event_generation', '构建任务相关的世界事件', line.strip())
    if 'Phase 2: Task Types' in line:
        return ('task_generation', '构建任务证据与约束', '事件准备完成，开始生成任务')
    if line.lstrip().startswith('--- ['):
        return ('task_generation', '构建任务证据与约束', line.strip('- '))
    if 'canonical' in line.lower() or 'contract validation' in line.lower():
        return ('validation', '校验任务产物契约', line.strip())
    return None
_BUILD_PROGRESS_RE = re.compile('\\[BuildProgress\\]\\s+phase=evidence\\s+accepted=(\\d+)\\s+requested=(\\d+)')
_BUILD_DETAIL_RE = re.compile('\\[BuildProgress\\]\\s+phase=detail\\s+task=(\\d+)\\s+accepted=(\\d+)\\s+requested=(\\d+)\\s+step=([a-z_]+)\\s+state=([a-z_]+)(?:\\s+note=([^\\s]+))?')
_TASK_PROGRESS_RE = re.compile('\\[TaskProgress\\]\\s+phase=(candidate|contract|repair)\\s+(?:(?:written=(\\d+))|(?:valid=(\\d+)\\s+rejected=(\\d+))|(?:round=(\\d+)/2\\s+missing=(\\d+)))\\s+requested=(\\d+)')
_LOG_SEPARATOR_RE = re.compile('^(?:\\d{2}:\\d{2}:\\d{2}\\s+)?[━─=\\-]{3,}\\s*$')

def _is_generation_log_separator(line: str) -> bool:
    """Keep logger decoration in diagnostics without exposing it as progress."""
    return bool(_LOG_SEPARATOR_RE.fullmatch(str(line or '').strip()))

def _generation_construction_progress_from_output(line: str) -> tuple[int, int] | None:
    """Read an evidence-pipeline checkpoint before its task folder exists."""
    match = _BUILD_PROGRESS_RE.search(line)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))

def _generation_build_detail_from_output(line: str) -> dict[str, int | str] | None:
    """Read a bounded, non-sensitive substep emitted during evidence construction."""
    match = _BUILD_DETAIL_RE.search(line)
    if not match:
        return None
    return {'task_index': int(match.group(1)), 'accepted': int(match.group(2)), 'requested': int(match.group(3)), 'step': match.group(4), 'state': match.group(5), 'note': match.group(6) or ''}

def _describe_generation_build_detail(progress: dict[str, int | str]) -> str:
    """Translate stable worker events into concise operator-facing progress."""
    task_index = int(progress['task_index'])
    step = str(progress['step'])
    state = str(progress['state'])
    note = str(progress.get('note') or '')
    labels = {'sampling': '选择候选地点', 'streetview_metadata': '查询 Google Street View 全景', 'streetview_image': '下载 Google Street View 图片', 'llm_prompt': '调用所选模型编写地点识别题面', 'quality_gate': '执行证据质量门禁', 'candidate': '更换候选地点'}
    states = {'running': '正在', 'complete': '已完成', 'failed': '未完成', 'retrying': '正在重试', 'fallback': '已改用本地回退'}
    suffix = f'（图片 {note}）' if step == 'streetview_image' and note else ''
    if note.startswith('attempt='):
        suffix = f"（第 {note.split('=', 1)[1]} 次尝试）"
    return f'第 {task_index} 条：{states.get(state, state)}{labels.get(step, step)}{suffix}'

def _generation_task_progress_from_output(line: str) -> dict[str, int | str] | None:
    """Read per-task formalization checkpoints emitted by every generator."""
    match = _TASK_PROGRESS_RE.search(line)
    if not match:
        return None
    phase = match.group(1)
    requested = int(match.group(7))
    if phase == 'candidate':
        return {'phase': phase, 'candidate_count': int(match.group(2)), 'requested': requested}
    if phase == 'contract':
        return {'phase': phase, 'completed_count': int(match.group(3)), 'rejected_count': int(match.group(4)), 'requested': requested}
    return {'phase': phase, 'repair_round': int(match.group(5)), 'missing_count': int(match.group(6)), 'requested': requested}

def _stream_generation_output(stream, lines: queue.Queue[str]) -> None:
    try:
        for line in iter(stream.readline, ''):
            lines.put(line.rstrip())
    finally:
        stream.close()

def _run_generation_job(job: dict, command: list[str]) -> None:
    """Execute one build and publish progress without blocking the HTTP handler."""
    log_path = Path(job['_log_path'])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    output_lines: list[str] = []
    lines: queue.Queue[str] = queue.Queue()
    try:
        if job.get('status') == 'cancelling':
            _update_generation_job(job, status='cancelled', phase='cancelled', phase_label='本次构建已停止', detail='构建进程启动前已取消，未调用外部模型或地图服务')
            return
        _update_generation_job(job, status='running', phase='event_generation', phase_label='准备静态任务资料', detail='正在读取城市世界与任务配置')
        proc = subprocess.Popen(command, cwd=str(PIPELINE_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, env=_generation_subprocess_env({'GEOWORD_ROUTE_COST_RUN_ID': str(job['job_id'])}))
        with _GENERATION_JOB_LOCK:
            _GENERATION_PROCESSES[job['job_id']] = proc
        reader = threading.Thread(target=_stream_generation_output, args=(proc.stdout, lines), daemon=True)
        reader.start()
        with log_path.open('w', encoding='utf-8') as log_file:
            while proc.poll() is None:
                try:
                    line = lines.get(timeout=1)
                except queue.Empty:
                    line = ''
                if line:
                    safe_line = _sanitize_external_error(line)
                    output_lines.append(safe_line)
                    log_file.write(safe_line + '\n')
                    log_file.flush()
                    phase = _generation_phase_from_output(safe_line)
                    construction = _generation_construction_progress_from_output(safe_line)
                    build_detail = _generation_build_detail_from_output(safe_line)
                    task_progress = _generation_task_progress_from_output(safe_line)
                    updates = {}
                    if phase:
                        updates.update(phase=phase[0], phase_label=phase[1], detail=phase[2])
                    if construction:
                        (accepted, requested) = construction
                        requested = max(1, job['requested_count'])
                        accepted = min(accepted, requested)
                        updates.update(phase='task_generation', phase_label='构建任务证据与约束', construction_count=max(job.get('construction_count', 0), accepted), detail=f'已完成第 {accepted}/{requested} 条任务证据，等待写入并校验任务产物')
                    if build_detail:
                        requested = max(1, job['requested_count'])
                        accepted = min(int(build_detail['accepted']), requested)
                        updates.update(phase='task_generation', phase_label='构建任务证据与约束', construction_count=max(job.get('construction_count', 0), accepted), detail=_describe_generation_build_detail(build_detail))
                    if task_progress:
                        requested = max(1, job['requested_count'])
                        progress_phase = str(task_progress['phase'])
                        if progress_phase == 'candidate':
                            candidate = min(int(task_progress['candidate_count']), requested)
                            updates.update(phase='validation', phase_label='逐题校验任务产物', candidate_count=max(job.get('candidate_count', 0), candidate), detail=f'已写入候选 {candidate}/{requested}，正在逐题校验')
                        elif progress_phase == 'contract':
                            valid_count = min(int(task_progress['completed_count']), requested)
                            rejected_count = int(task_progress['rejected_count'])
                            updates.update(phase='validation', phase_label='逐题校验任务产物', completed_count=max(job.get('completed_count', 0), valid_count), rejected_count=max(job.get('rejected_count', 0), rejected_count), detail=f'有效任务 {valid_count}/{requested}' + (f'，已拒绝 {rejected_count} 条候选' if rejected_count else ''))
                        else:
                            missing = int(task_progress['missing_count'])
                            updates.update(phase='task_generation', phase_label='补齐有效任务缺口', detail=f"第 {task_progress['repair_round']}/2 轮补齐：仍缺 {missing} 条有效任务")
                    if updates:
                        _update_generation_job(job, **updates)
                    elif not _is_generation_log_separator(safe_line):
                        _update_generation_job(job, detail=safe_line[-240:])
                (observed, valid, rejected) = _generation_task_progress(job)
                if observed != job.get('_observed_folders', []) or valid != job.get('generated_folders', []) or rejected != job.get('rejected_count', 0):
                    job['_observed_folders'] = observed
                    _update_generation_job(job, observed_count=min(len(observed), job['requested_count']), candidate_count=max(job.get('candidate_count', 0), min(len(observed), job['requested_count'])), completed_count=min(len(valid), job['requested_count']), rejected_count=rejected, generated_folders=valid)
            reader.join(timeout=2)
            while not lines.empty():
                safe_line = _sanitize_external_error(lines.get_nowait())
                output_lines.append(safe_line)
                log_file.write(safe_line + '\n')
            log_file.flush()
        (observed, valid, rejected) = _generation_task_progress(job)
        job['_observed_folders'] = observed
        was_cancelled = job.get('status') == 'cancelling'
        return_code = proc.returncode
        preserved_detail = _generation_preservation_detail(valid)
        if was_cancelled:
            _update_generation_job(job, status='cancelled', phase='cancelled', phase_label='本次构建已停止', detail=preserved_detail, observed_count=min(len(observed), job['requested_count']), candidate_count=max(job.get('candidate_count', 0), min(len(observed), job['requested_count'])), completed_count=min(len(valid), job['requested_count']), rejected_count=rejected, generated_folders=valid, diagnostic_log=str(log_path.relative_to(PIPELINE_ROOT)).replace('\\', '/'))
        elif return_code == 0 and len(valid) >= job['requested_count']:
            _update_generation_job(job, status='completed', phase='completed', phase_label='构建完成', detail='所有任务已通过产物契约校验', observed_count=min(len(observed), job['requested_count']), candidate_count=max(job.get('candidate_count', 0), min(len(observed), job['requested_count'])), completed_count=min(len(valid), job['requested_count']), rejected_count=rejected, generated_folders=valid, diagnostic_log=str(log_path.relative_to(PIPELINE_ROOT)).replace('\\', '/'))
        elif return_code == 0:
            _update_generation_job(job, status='completed_with_shortfall', phase='validation', phase_label='构建结束，但有效任务数量不足', detail=f"请求 {job['requested_count']} 条，已通过校验 {len(valid)} 条", observed_count=min(len(observed), job['requested_count']), candidate_count=max(job.get('candidate_count', 0), min(len(observed), job['requested_count'])), completed_count=min(len(valid), job['requested_count']), rejected_count=rejected, generated_folders=valid, diagnostic_log=str(log_path.relative_to(PIPELINE_ROOT)).replace('\\', '/'))
        else:
            diagnostic = _describe_external_failure('\n'.join(output_lines[-120:]), job['model_label'])
            _update_generation_job(job, status='failed', phase='failed', phase_label='构建中断', detail=preserved_detail, error=diagnostic['message'], observed_count=min(len(observed), job['requested_count']), completed_count=min(len(valid), job['requested_count']), generated_folders=valid, diagnostic_log=str(log_path.relative_to(PIPELINE_ROOT)).replace('\\', '/'))
    except Exception as exc:
        _update_generation_job(job, status='failed', phase='failed', phase_label='无法启动构建任务', detail='后台构建进程未能启动', error=_sanitize_external_error(str(exc)))
    finally:
        with _GENERATION_JOB_LOCK:
            _GENERATION_PROCESSES.pop(job['job_id'], None)

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

def _resolve_generation_append_batch(root, task_type, city, world_id, requested, available):
    """An explicit failed batch still owns its seed even with zero formal tasks."""
    if requested and Path(requested).name == requested and (requested not in {'.', '..'}):
        folder = managed_batch_dir(root, task_type, world_id, city, requested)
        if (folder / 'batch_metadata.json').is_file() and _batch_version_compatibility(folder, world_id).get('compatible'):
            return requested
    known = {entry['name'] for entry in available['batches']}
    return requested if requested in known else available.get('current', '')

def _list_batches(benchmarks_root: str, task_type: str='', city: str='', active_world_id: str='', *, include_history: bool=False) -> dict:
    """Return history for one task type and city, never a mixed global list."""
    if not active_world_id or not task_type or (not city):
        return {'batches': [], 'current': ''}
    entries: list[dict] = []
    for candidate in iter_batches(benchmarks_root, task_type=task_type, world_id=active_world_id, city_scope=city):
        compatible = _batch_version_compatibility(candidate, active_world_id).get('compatible')
        if not compatible and (not include_history):
            continue
        metadata = _batch_metadata(candidate)
        formal_count = sum((1 for task_path in task_dirs(candidate) if recorded_formal_task(task_path)))
        visible_count = len(task_dirs(candidate)) if include_history else formal_count
        if not visible_count:
            continue
        entries.append({'name': candidate.name, 'task_count': visible_count, 'history_only_count': visible_count - formal_count if compatible else visible_count, 'is_latest': False, 'world_id': active_world_id, 'city_id': candidate.parent.name, 'task_type': candidate.parents[2].name, 'display_name': metadata.get('display_name') or '正式任务构建', 'created_at': metadata.get('created_at', ''), 'lifecycle': metadata.get('lifecycle', 'formal_pending_review')})
    entries.sort(key=lambda item: (item['created_at'], item['name']), reverse=True)
    current_batches = _select_batches(benchmarks_root, task_type=task_type, city_id=city, world_id=active_world_id, target_batch='latest', include_history=include_history)
    current = current_batches[0].name if current_batches else ''
    for entry in entries:
        entry['is_latest'] = entry['name'] == current
    return {'batches': entries, 'current': current}

def _batch_version_compatibility(batch_dir: Path, world_id: str) -> dict:
    """Check a batch sidecar against the World Studio's active city versions."""
    provenance_path = batch_dir / 'world_provenance.json'
    if not provenance_path.is_file():
        return {'compatible': False, 'reason': '任务批次缺少 world_provenance.json，无法验证城市版本。'}
    try:
        provenance = read_json(provenance_path)
        context = city_version_context(world_id)
    except Exception as exc:
        return {'compatible': False, 'reason': f'无法读取版本上下文：{exc}'}
    if provenance.get('world_id') != world_id:
        return {'compatible': False, 'reason': f"任务属于 {provenance.get('world_id') or '未绑定世界'}，当前世界是 {world_id}。"}
    requirements = provenance.get('required_city_versions') or {}
    if not requirements:
        return {'compatible': False, 'reason': '任务批次缺少所需城市版本，无法保证可复现性。'}
    active = context.get('active_city_versions') or {}
    unmet = [f'{city_id} 需要 v{required}，当前为 v{active.get(city_id, 0)}' for (city_id, required) in requirements.items() if int(active.get(city_id, 0) or 0) < int(required or 0)]
    if unmet:
        return {'compatible': False, 'reason': '；'.join(unmet), 'required_city_versions': requirements, 'active_city_versions': active}
    return {'compatible': True, 'required_city_versions': requirements, 'active_city_versions': active}

def _load_generation_job(job_id: str='') -> dict | None:
    with _GENERATION_JOB_LOCK:
        if job_id and job_id in _GENERATION_JOBS:
            return _generation_snapshot(_GENERATION_JOBS[job_id])
        if not job_id:
            active = _active_generation_job()
            if active:
                return _generation_snapshot(active)
    path = _generation_jobs_root() / f'{job_id}.json' if job_id else None
    if path and path.exists():
        try:
            return read_json(path)
        except Exception:
            return None
    return None

class GenerationHTTP(WorkbenchHTTP):

    def _json(self, payload, status=200):
        send(self.handler, payload, status)

    def handle(self, method, path):
        if path == '/api/generate/status':
            job_id = self.params.get('job_id', '')
            if job_id and (not re.fullmatch('[A-Za-z0-9_-]+', job_id)):
                self._error('Invalid job identifier.', 400)
                return
            self._json({'job': _load_generation_job(job_id)})
        elif path == '/api/generate':
            self._handle_generate()
        elif path == '/api/generate/cancel':
            self._cancel_generate()

    def _handle_generate(self) -> None:
        """POST /api/generate — start one observable background build job."""
        payload = self._read_json_body()
        if payload is None:
            return
        task_type = payload.get('task_type', '')
        if task_type != 'place_grounding':
            self._error('Pending migration and integration for this task type.', 501)
            return
        count = int(payload.get('count', 5))
        mode = payload.get('mode', 'append')
        city = str(payload.get('city') or '').strip()
        stage_model = payload.get('stage_model') or 'task_design'
        infeasible_percent = payload.get('infeasible_percent', 0)
        requested_batch = str(payload.get('batch') or '')
        world_id = self._current_browsing_world_id()
        route_diffusion_start = payload.get('route_diffusion_start')
        route_pool_max = payload.get('route_pool_max')
        route_cost_events_path: Path | None = None
        dynamic_admission = payload.get('dynamic_pretask_admission', 'all_validated_static')
        dynamic_inventory = None
        if not task_type or count < 1:
            self._error('task_type is required and count must be positive', 400)
            return
        if isinstance(infeasible_percent, bool) or not isinstance(infeasible_percent, int) or (not 0 <= infeasible_percent <= 100):
            self._error('infeasible_percent must be an integer from 0 to 100', 400)
            return
        if infeasible_percent and task_type not in _MIXED_FEASIBILITY_TASK_TYPES:
            self._error('Mixed feasibility construction is not enabled for any task type yet', 400)
            return
        if not city:
            self._error('请选择当前世界中的一个城市后再生成任务。', 400)
            return
        registered_cities = {item['city_id'] for item in self._current_world_cities()}
        if city not in registered_cities:
            self._error('所选城市不属于当前世界，或该城市当前不可用于生成任务。', 400)
            return
        allowed_models = {item['endpoint_key'] for item in _model_options()['generation']}
        if stage_model not in allowed_models:
            self._error('Unsupported generation model selection', 400)
            return
        model_label = _model_label(_model_options()['generation'], stage_model, 'endpoint_key')
        with _GENERATION_JOB_LOCK:
            active = _active_generation_job()
            if active:
                self._json({'ok': False, 'error': '已有任务构建正在运行，请先等待完成或停止当前构建。', 'error_code': 'generation_in_progress', 'job': _generation_snapshot(active)}, 409)
                return
        if mode == 'append':
            available = _list_batches(self.benchmarks_root, task_type, city, world_id)
            batch = _resolve_generation_append_batch(self.benchmarks_root, task_type, city, world_id, requested_batch, available)
            if not batch and available['batches']:
                batch = available['batches'][0]['name']
            if not batch:
                self._json({'ok': False, 'error': '当前城市和任务类型没有可沿用的批次，请选择“新建批次”。', 'error_code': 'no_appendable_batch'}, 409)
                return
        else:
            batch = datetime.now().strftime('%Y%m%d_%H%M%S')
        batch_dir = managed_batch_dir(self.benchmarks_root, task_type, world_id, city, batch)
        tasks_dir = batch_dir / 'tasks'
        baseline_folders = [entry.name for entry in tasks_dir.iterdir()] if tasks_dir.exists() else []
        generator_target_count = _generation_target_count(batch_dir, task_type, mode, count)
        job_id = f"gen_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        jobs_root = _generation_jobs_root()
        job = {'job_id': job_id, 'status': 'queued', 'phase': 'queued', 'phase_label': '等待启动构建', 'detail': '正在准备后台构建进程', 'task_type': task_type, 'city': city, 'world_id': world_id, 'batch': batch, 'mode': mode, 'model_label': model_label, 'requested_count': count, 'generator_target_count': generator_target_count, 'infeasible_percent': infeasible_percent, 'construction_count': 0, 'candidate_count': 0, 'completed_count': 0, 'observed_count': 0, 'rejected_count': 0, 'generated_folders': [], 'baseline_folders': baseline_folders, '_observed_folders': [], 'started_at': datetime.now().isoformat(timespec='seconds'), 'last_activity_at': datetime.now().isoformat(timespec='seconds'), '_started_monotonic': time.monotonic(), 'output_dir': str(batch_dir), '_state_path': str(jobs_root / f'{job_id}.json'), '_log_path': str(jobs_root / f'{job_id}.log'), 'route_cost_events_path': str(route_cost_events_path) if route_cost_events_path else ''}
        cmd = [sys.executable, '-m', 'task_module.generator', '--city', city, '--world-id', world_id, '--types', task_type, '--counts', str(generator_target_count), '--output', 'outputs/benchmarks', '--stage-model', stage_model, '--batch', batch, '--infeasible-percent', str(infeasible_percent)]
        if mode == 'new':
            cmd.append('--new-batch')
        with _GENERATION_JOB_LOCK:
            _GENERATION_JOBS[job_id] = job
            _persist_generation_job(job)
        threading.Thread(target=_run_generation_job, args=(job, cmd), daemon=True).start()
        self._json({'ok': True, 'job': _generation_snapshot(job)})

    def _cancel_generate(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        job_id = str(payload.get('job_id') or '')
        with _GENERATION_JOB_LOCK:
            job = _GENERATION_JOBS.get(job_id)
            proc = _GENERATION_PROCESSES.get(job_id)
        if not job or job.get('status') not in _GENERATION_ACTIVE_STATES:
            self._error('No active generation job matches job_id', 404)
            return
        _update_generation_job(job, status='cancelling', phase='cancelling', phase_label='正在停止本次构建', detail='正在保留已完整落盘的任务')
        if proc and proc.poll() is None:
            proc.terminate()
        self._json({'ok': True, 'job': _generation_snapshot(job)})
