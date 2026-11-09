"""Background generation for supported task types."""
from __future__ import annotations
import copy,hashlib,json,math,os,queue,re,secrets,shutil,socket,subprocess,sys,threading,time,uuid
from typing import Any
from world_module.registry import city_version_context, resolve_world_route_cache
from task_module._shared.batch_state import next_task_index
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

from world_module.registry import materialize_city_service_detail_snapshots
from world_module.service_detail_snapshot import identity_key, public_identity, read_service_detail_checkpoint, sha256_file, website_uri
from task_module.task_types.itinerary.cost_planner import build_itinerary_cost_preview, itinerary_routing_place_ids
from task_module.task_types.region_aware.cost_planner import build_region_cost_preview, region_routing_place_ids

# Existing route-cost display constant; Grounding itself makes no route calls.
from task_module.task_types.route_planning.cost_planner import UNIT_PRICE_USD, build_route_cost_preview
from task_module.task_types.multi_entity.cost_planner import build_multi_entity_cost_preview
from task_module.task_types.safety_planning.cost_planner import build_safety_cost_preview, safety_routing_place_ids

_ROUTE_COST_PREFLIGHTS: dict[str, dict] = {}
_SAFETY_COST_PREFLIGHTS: dict[str, dict] = {}
_ITINERARY_COST_PREFLIGHTS: dict[str, dict] = {}
_MULTI_ENTITY_COST_PREFLIGHTS: dict[str, dict] = {}
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

def _route_cache_pool_size(cache_payload: dict, pool_payload: dict, world: dict | None = None) -> int:
    """Count version-bound route POIs without requiring a migrated pool file."""
    place_ids = [str(value) for value in pool_payload.get("place_ids") or [] if value]
    if place_ids:
        return len(set(place_ids))
    places = (world or {}).get("places") or []
    valid_ids = {str(place.get("place_id") or "") for place in places if place.get("place_id")}
    coordinate_to_id = {
        (round(float(location["lat"]), 6), round(float(location["lng"]), 6)): str(place.get("place_id") or "")
        for place in places
        if isinstance((location := place.get("location")), dict)
        and location.get("lat") is not None and location.get("lng") is not None
        and place.get("place_id")
    }
    endpoints: set[str] = set()
    for row in cache_payload.get("cache") or []:
        if not isinstance(row, dict):
            continue
        for prefix in ("origin", "dest"):
            place_id = str(row.get(f"{prefix}_place_id") or "").strip()
            if place_id and (not valid_ids or place_id in valid_ids):
                endpoints.add(place_id)
                continue
            lat = row.get(f"{prefix}_lat")
            lng = row.get(f"{prefix}_lng")
            if lat is not None and lng is not None:
                try:
                    coordinate = (round(float(lat), 6), round(float(lng), 6))
                    canonical_id = coordinate_to_id.get(coordinate)
                    if canonical_id:
                        endpoints.add(canonical_id)
                except (TypeError, ValueError):
                    continue
    return len(endpoints)


def _character_route_fact_count(entries: list[dict], character_ids: set[str]) -> int:
    """Count character-origin routes across canonical and legacy ID forms."""
    compatible_ids = character_ids | {f"character:{character_id}" for character_id in character_ids}
    return sum(
        1 for entry in entries
        if str(entry.get("origin_place_id") or "") in compatible_ids
    )


def _multi_entity_effective_route_payload(route_path: Path | None) -> dict:
    """Read the same durable main+journal cache view used by formal generation."""
    payload = read_json(route_path) if route_path and route_path.is_file() else {}
    merged: dict[tuple[str, str, str, str, str], dict] = {}

    def merge_entry(entry: object) -> None:
        if not isinstance(entry, dict):
            return
        route = None if entry.get("route_ref") else entry.get("route", {})
        if route is not None and route.get("distance_m", 0) == 0 and "available" not in route:
            return
        key = (
            str(entry.get("origin_place_id") or ""),
            str(entry.get("dest_place_id") or ""),
            str(entry.get("mode") or ""),
            str(entry.get("waypoint_place_id") or ""),
            str(entry.get("snapshot_id") or "static_v1"),
        )
        merged[key] = entry

    for entry in payload.get("cache") or []:
        merge_entry(entry)
    pending_path = route_path.parent / "route_cache.pending.jsonl" if route_path else None
    if pending_path and pending_path.is_file():
        try:
            with pending_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        merge_entry(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass

    effective = dict(payload)
    effective["cache"] = list(merged.values())
    effective["cached_pair_count"] = len(merged)
    return effective


def _multi_entity_routing_place_ids(
    cache_payload: dict, pool_payload: dict, world: dict,
) -> list[str]:
    """Resolve Routing POIs for Multi-Entity, including legacy caches without a pool file."""
    places = [place for place in world.get("places") or [] if isinstance(place, dict)]
    world_ids = {
        str(place.get("place_id") or "") for place in places if place.get("place_id")
    }
    manifest_ids = {
        str(place_id) for place_id in pool_payload.get("place_ids") or []
        if str(place_id) in world_ids
    }
    if manifest_ids:
        return sorted(manifest_ids)

    coordinate_to_id = {
        (round(float(location["lat"]), 6), round(float(location["lng"]), 6)):
            str(place.get("place_id") or "")
        for place in places
        if isinstance((location := place.get("location")), dict)
        and location.get("lat") is not None and location.get("lng") is not None
        and place.get("place_id")
    }
    legacy_ids: set[str] = set()
    for entry in cache_payload.get("cache") or []:
        if not isinstance(entry, dict):
            continue
        for prefix in ("origin", "dest"):
            place_id = str(entry.get(f"{prefix}_place_id") or "")
            if place_id in world_ids:
                legacy_ids.add(place_id)
                continue
            try:
                mapped = coordinate_to_id.get((
                    round(float(entry.get(f"{prefix}_lat")), 6),
                    round(float(entry.get(f"{prefix}_lng")), 6),
                ))
            except (TypeError, ValueError):
                mapped = None
            if mapped:
                legacy_ids.add(mapped)
    return sorted(legacy_ids)


_REGION_COST_PREFLIGHTS: dict[str, dict] = {}

_SERVICE_DETAIL_PREFLIGHTS: dict[str, dict] = {}

_SERVICE_DETAIL_JOBS: dict[str, dict] = {}

_SERVICE_DETAIL_JOB_LOCK = threading.RLock()

_REGION_BUILD_JOBS: dict[str, dict] = {}

_REGION_BUILD_PROCESSES: dict[str, subprocess.Popen] = {}

_REGION_BUILD_LOCK = threading.RLock()

_REGION_BUILD_BATCHES: set[str] = set()

_REGION_BUILD_CANCEL_REQUESTS: set[str] = set()

_REGION_RETRY_DELAYS = (5, 10, 20, 40, 60, 120, 240)

_REGION_MODEL_RETRY_DELAYS = (30, 60, 120, 240)

def _process_is_running(process_id: object) -> bool:
    """Return whether a persisted child PID still identifies a live process."""
    try:
        pid = int(process_id or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, PermissionError):
        return False
    return True

def _region_retry_delay(attempt: int) -> int:
    index = min(max(int(attempt), 1), len(_REGION_RETRY_DELAYS)) - 1
    return _REGION_RETRY_DELAYS[index]

def _region_retry_attempt(job: dict, completed: int) -> int:
    previous_progress = int(job.get("retry_progress_completed") or 0)
    previous_attempt = int(job.get("retry_attempt") or 0)
    return 1 if completed > previous_progress else previous_attempt + 1

def _region_model_retry_delay(attempt: int) -> int:
    index = min(max(int(attempt), 1), len(_REGION_MODEL_RETRY_DELAYS)) - 1
    return _REGION_MODEL_RETRY_DELAYS[index]


def _clear_region_model_retry_state(job: dict) -> None:
    """Drop an internal retry notice once a later item completion proves recovery."""
    job.pop("internal_model_retry_until", None)
    job.pop("internal_model_retry_attempt", None)
    job.pop("internal_model_retry_max", None)
    job.pop("model_retry_item", None)

def _select_region_source_page_title(
    selection: dict, prior: dict, legacy_evidence: dict, city: dict, city_id: str,
) -> str:
    """Choose the strongest persisted source-page identity before name fallback."""
    return str(
        selection.get("page_title") or prior.get("page_title") or
        legacy_evidence.get("page_title") or city.get("city_name") or city_id
    ).strip()

def _region_build_worker(job: dict, command: list[str]) -> None:
    """Run one already-confirmed Region source stage without changing its contract."""
    state_path = Path(job["state_path"])
    log_path = Path(job["log_path"])
    try:
        job.update({"status": "running", "updated_at": datetime.now().isoformat(timespec="seconds")})
        write_json(state_path, {key: value for key, value in job.items() if not key.endswith("_command")})
        process = subprocess.Popen(
            command, cwd=str(PIPELINE_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=_generation_subprocess_env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with _REGION_BUILD_LOCK:
            _REGION_BUILD_PROCESSES[job["city_id"]] = process
        job["process_pid"] = process.pid
        write_json(state_path, {key: value for key, value in job.items() if not key.endswith("_command")})
        with log_path.open("a", encoding="utf-8") as log_file:
            for line in process.stdout or []:
                log_file.write(line)
                log_file.flush()
                phase_event = re.search(r"region_phase\s+(\w+)", line)
                if phase_event:
                    phase = phase_event.group(1)
                    job.update({
                        "phase": phase,
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    })
                    stage_key = {
                        "media_fetch": "media",
                        "stage1_extraction": "stage1",
                        "stage2_assessment": "stage2",
                    }.get(phase)
                    if stage_key:
                        job["completed"] = int(job.get(f"{stage_key}_completed") or 0)
                        job["total"] = int(job.get(f"{stage_key}_total") or 0)
                    else:
                        job["completed"] = 0
                        job["total"] = 0
                    job.pop("current_item", None)
                    job.pop("internal_retry_until", None)
                    job.pop("internal_model_retry_until", None)
                    job.pop("internal_model_retry_attempt", None)
                    job.pop("internal_model_retry_max", None)
                    job.pop("model_retry_item", None)
                    write_json(state_path, {key: value for key, value in job.items() if not key.endswith("_command")})
                item_event = re.search(r"region_item\s+(\w+)\s+(\d+)/(\d+)\s+(.+)", line)
                if item_event:
                    try:
                        item_name = json.loads(item_event.group(4))
                    except json.JSONDecodeError:
                        item_name = item_event.group(4).strip()
                    job.update({
                        "phase": item_event.group(1), "current_item": str(item_name),
                        "current_index": int(item_event.group(2)), "current_total": int(item_event.group(3)),
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    })
                    job.pop("internal_retry_until", None)
                    job.pop("internal_model_retry_until", None)
                    job.pop("internal_model_retry_attempt", None)
                    job.pop("internal_model_retry_max", None)
                    job.pop("model_retry_item", None)
                    write_json(state_path, {key: value for key, value in job.items() if not key.endswith("_command")})
                retry_event = re.search(r"media_retry\s+(\d+)/(\d+)\s+([0-9.]+)\s+(.+)", line)
                if retry_event:
                    try:
                        retry_item = json.loads(retry_event.group(4))
                    except json.JSONDecodeError:
                        retry_item = retry_event.group(4).strip()
                    delay = float(retry_event.group(3))
                    job.update({
                        "phase": "media_fetch", "current_item": str(retry_item),
                        "internal_retry_attempt": int(retry_event.group(1)),
                        "internal_retry_max": int(retry_event.group(2)),
                        "internal_retry_until": time.time() + delay,
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    })
                    write_json(state_path, {key: value for key, value in job.items() if not key.endswith("_command")})
                model_retry_event = re.search(r"model_retry\s+(\d+)/(\d+)\s+([0-9.]+)\s+(.+)", line)
                if model_retry_event:
                    try:
                        retry_item = json.loads(model_retry_event.group(4))
                    except json.JSONDecodeError:
                        retry_item = model_retry_event.group(4).strip()
                    delay = float(model_retry_event.group(3))
                    model_phase = "stage1_extraction" if job.get("stage") == "stage1" else "stage2_assessment"
                    job.update({
                        "phase": model_phase, "model_retry_item": str(retry_item),
                        "internal_model_retry_attempt": int(model_retry_event.group(1)),
                        "internal_model_retry_max": int(model_retry_event.group(2)),
                        "internal_model_retry_until": time.time() + delay,
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    })
                    write_json(state_path, {key: value for key, value in job.items() if not key.endswith("_command")})
                progress = re.match(r"^(media_fetch|stage1_extraction|stage2_assessment)\s+(\d+)/(\d+)\s*$", line)
                if progress:
                    job.update({"phase": progress.group(1), "completed": int(progress.group(2)), "total": int(progress.group(3)),
                                "updated_at": datetime.now().isoformat(timespec="seconds")})
                    if progress.group(1) in {"stage1_extraction", "stage2_assessment"}:
                        _clear_region_model_retry_state(job)
                    stage_key = {"media_fetch": "media", "stage1_extraction": "stage1", "stage2_assessment": "stage2"}[progress.group(1)]
                    job[f"{stage_key}_completed"] = int(progress.group(2))
                    job[f"{stage_key}_total"] = int(progress.group(3))
                    write_json(state_path, {key: value for key, value in job.items() if not key.endswith("_command")})
        return_code = process.wait()
        with _REGION_BUILD_LOCK:
            cancel_requested = job["world_id"] in _REGION_BUILD_CANCEL_REQUESTS
        if cancel_requested:
            job.update({
                "status": "interrupted",
                "error": "已人工中断当前 Region 城市队列，可从已有缓存继续构建。",
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            return
        if return_code:
            manifest_path = Path(job["output_root"]) / "run_manifest.json"
            manifest = read_json(manifest_path) if manifest_path.is_file() else {}
            failures = [item for item in manifest.get("failures") or [] if isinstance(item, dict)]
            latest_failure = failures[-1] if failures else {}
            if latest_failure.get("stage") == "media_fetch" and latest_failure.get("retryable") is True:
                progress_state = manifest.get("progress") or {}
                completed = int(progress_state.get("completed") or 0)
                total = int(progress_state.get("total") or 0)
                file_reference = str(latest_failure.get("file_reference") or latest_failure.get("item_id") or "")
                retry_attempt = _region_retry_attempt(job, completed)
                retry_delay = _region_retry_delay(retry_attempt)
                job.update({
                    "status": "retry_wait", "phase": "media_fetch",
                    "completed": completed, "total": total,
                    "media_completed": completed, "media_total": total,
                    "retry_attempt": retry_attempt, "retry_delay": retry_delay,
                    "retry_progress_completed": completed,
                    "retry_remaining": retry_delay, "retry_file": file_reference,
                    "error_code": "wikimedia_media_unavailable",
                    "media_failure_phase": str(latest_failure.get("media_phase") or "media_fetch"),
                    "error": "Wikimedia 图片服务暂时不可用，正在从已保存进度自动恢复。",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                })
                return
            if latest_failure.get("stage") == "media_fetch":
                job.update({
                    "status": "interrupted", "phase": "media_fetch",
                    "retry_file": str(latest_failure.get("file_reference") or latest_failure.get("item_id") or ""),
                    "media_failure_phase": str(latest_failure.get("media_phase") or "media_processing"),
                    "error_code": "wikimedia_media_invalid",
                    "error": f"图片无法自动恢复：{latest_failure.get('error') or '文件不可用或格式不受支持'}",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                })
                return
            expected_model_phase = "stage1_extraction" if job.get("stage") == "stage1" else "stage2_assessment"
            if (latest_failure.get("stage") == expected_model_phase
                    and latest_failure.get("failure_kind") == "infrastructure"):
                progress_state = manifest.get("progress") or {}
                stage_key = "stage1" if job.get("stage") == "stage1" else "stage2"
                completed = int(progress_state.get("completed") or job.get(f"{stage_key}_completed") or 0)
                total = int(progress_state.get("total") or job.get(f"{stage_key}_total") or 0)
                retry_attempt = int(job.get("model_retry_attempt") or 0) + 1
                retry_delay = _region_model_retry_delay(retry_attempt)
                failure_is_timeout = latest_failure.get("error_type") == "TimeoutError"
                job.update({
                    "status": "retry_wait", "phase": expected_model_phase,
                    "completed": completed, "total": total,
                    f"{stage_key}_completed": completed, f"{stage_key}_total": total,
                    "model_retry_attempt": retry_attempt, "retry_delay": retry_delay,
                    "retry_remaining": retry_delay,
                    "retry_item": str(latest_failure.get("item_id") or job.get("current_item") or ""),
                    "error_code": "model_infrastructure_unavailable",
                    "model_failure_reason": "timeout" if failure_is_timeout else "connection",
                    "error": (
                        "模型响应超时，将自动从已有缓存继续。" if failure_is_timeout else
                        "模型服务连接暂时不可用，将自动从已有缓存继续。"
                    ),
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                })
                return
            raise RuntimeError(f"Region {job['stage']} process exited with code {return_code}")
        manifest_path = Path(job["output_root"]) / "run_manifest.json"
        manifest = read_json(manifest_path) if manifest_path.is_file() else {}
        actual_stage = str((manifest.get("result") or {}).get("stage") or "")
        expected_stage = f"{job['stage']}_complete"
        if actual_stage != expected_stage:
            job.update({"status": "interrupted", "phase": actual_stage or f"{job['stage']}_partial",
                        "error": "本次调用额度已结束或阶段尚未完成，可继续当前阶段。",
                        "updated_at": datetime.now().isoformat(timespec="seconds")})
            return
        result = manifest.get("result") or {}
        if job["stage"] == "stage1":
            stage_total = int(manifest.get("chunks") or job.get("total") or 0)
        else:
            stage_total = int(result.get("stage2_input_rules") or job.get("total") or 0)
        job[f"{job['stage']}_completed"] = stage_total
        job[f"{job['stage']}_total"] = stage_total
        if job["stage"] == "stage2":
            job.update({"phase": "quality_validation", "updated_at": datetime.now().isoformat(timespec="seconds")})
            write_json(state_path, {key: value for key, value in job.items() if not key.endswith("_command")})
            draft = Path(job["output_root"])
            rules = read_json(draft / "regional_rules.json")
            assessments = read_json(draft / "regional_rule_stage2_assessments.json")
            if rules.get("schema_version") != "regional_rules.v3" or assessments.get("schema_version") != "regional_rule_stage2_assessments.v4":
                raise ValueError("Region build completed with a non-current schema")
            rule_ids = _region_stage2_expected_rule_ids(rules)
            assessment_ids = {str(row.get("rule_id") or "") for row in assessments.get("assessments") or []}
            if not rule_ids or rule_ids != assessment_ids:
                raise ValueError("Region Stage 1 and Stage 2 rule IDs do not match")
            formal = Path(job["formal_root"])
            job.update({"phase": "formal_promotion", "updated_at": datetime.now().isoformat(timespec="seconds")})
            write_json(state_path, {key: value for key, value in job.items() if not key.endswith("_command")})
            formal.mkdir(parents=True, exist_ok=True)
            for name in ("wikivoyage_source_snapshot.json", "regional_rules.json", "regional_rule_stage2_assessments.json", "run_manifest.json"):
                shutil.copy2(draft / name, formal / name)
        job.update({"status": "completed", "phase": expected_stage, "updated_at": datetime.now().isoformat(timespec="seconds")})
    except Exception as exc:
        job.update({"status": "failed", "error": _sanitize_external_error(str(exc)), "updated_at": datetime.now().isoformat(timespec="seconds")})
    finally:
        job.pop("process_pid", None)
        write_json(state_path, {key: value for key, value in job.items() if not key.endswith("_command")})
        with _REGION_BUILD_LOCK:
            _REGION_BUILD_PROCESSES.pop(job["city_id"], None)

def _region_build_batch_worker(world_id: str, jobs: list[dict]) -> None:
    """Run selected cities serially and preserve the Stage 1 -> Stage 2 boundary."""
    try:
        for job in jobs:
            with _REGION_BUILD_LOCK:
                cancel_requested = world_id in _REGION_BUILD_CANCEL_REQUESTS
            if cancel_requested:
                job.update({
                    "status": "interrupted", "phase": job.get("phase") or "interrupted",
                    "error": "已人工中断当前 Region 城市队列，可从已有缓存继续构建。",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                })
                write_json(Path(job["state_path"]), {key: value for key, value in job.items() if not key.endswith("_command")})
                continue
            for stage in ("stage1", "stage2"):
                job["stage"] = stage
                command = list(job[f"{stage}_command"])
                while True:
                    with _REGION_BUILD_LOCK:
                        cancel_requested = world_id in _REGION_BUILD_CANCEL_REQUESTS
                    if cancel_requested:
                        job.update({
                            "status": "interrupted", "phase": job.get("phase") or "interrupted",
                            "error": "已人工中断当前 Region 城市队列，可从已有缓存继续构建。",
                            "updated_at": datetime.now().isoformat(timespec="seconds"),
                        })
                        write_json(Path(job["state_path"]), {key: value for key, value in job.items() if not key.endswith("_command")})
                        break
                    job.update({"status": "queued", "phase": f"{stage}_queued", "error": "", "completed": 0, "total": 0})
                    _region_build_worker(job, command)
                    if job.get("status") == "completed" and job.get("phase") == f"{stage}_complete":
                        break
                    if job.get("status") == "failed":
                        break
                    if job.get("status") == "retry_wait":
                        is_model_retry = job.get("error_code") == "model_infrastructure_unavailable"
                        retry_attempt = int(job.get("model_retry_attempt") if is_model_retry else job.get("retry_attempt") or 0)
                        retry_limit = 5 if is_model_retry else 8
                        if retry_attempt >= retry_limit:
                            job.update({
                                "status": "interrupted",
                                "error": (
                                    "模型服务连续连接失败，已暂停自动重试。稍后可继续，已有准则评估不会重复请求。"
                                    if is_model_retry else
                                    "Wikimedia 图片服务连续不可用，已暂停自动重试。稍后可继续当前阶段，已下载图片不会重复下载。"
                                ),
                                "updated_at": datetime.now().isoformat(timespec="seconds"),
                            })
                            job.pop("retry_remaining", None)
                            write_json(Path(job["state_path"]), {key: value for key, value in job.items() if not key.endswith("_command")})
                            break
                        retry_delay = max(1, int(job.get("retry_delay") or 5))
                        for remaining in range(retry_delay, 0, -1):
                            with _REGION_BUILD_LOCK:
                                if world_id in _REGION_BUILD_CANCEL_REQUESTS:
                                    job.update({
                                        "status": "interrupted",
                                        "error": "已人工中断当前 Region 城市队列，可从已有缓存继续构建。",
                                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                                    })
                                    write_json(Path(job["state_path"]), {key: value for key, value in job.items() if not key.endswith("_command")})
                                    break
                            job.update({"retry_remaining": remaining, "updated_at": datetime.now().isoformat(timespec="seconds")})
                            write_json(Path(job["state_path"]), {key: value for key, value in job.items() if not key.endswith("_command")})
                            time.sleep(1)
                        if job.get("status") == "interrupted":
                            break
                        job.pop("retry_remaining", None)
                        if is_model_retry and "--retry-failed" not in command:
                            command.append("--retry-failed")
                        continue
                    # A normal max-calls pause is resumed immediately using script caches.
                    if job.get("status") != "interrupted":
                        job.update({"status": "failed", "error": "Region 构建进入未知状态。"})
                        write_json(Path(job["state_path"]), {key: value for key, value in job.items() if not key.endswith("_command")})
                        break
                if job.get("status") == "completed":
                    job.pop("retry_attempt", None)
                    job.pop("retry_delay", None)
                    job.pop("retry_remaining", None)
                    job.pop("retry_progress_completed", None)
                    job.pop("model_retry_attempt", None)
                else:
                    # A city must finish both stages before the queue advances.
                    # Preserve all later jobs as queued so an operator can resume
                    # from this city's durable caches after fixing the failure.
                    return
    finally:
        with _REGION_BUILD_LOCK:
            _REGION_BUILD_BATCHES.discard(world_id)
            _REGION_BUILD_CANCEL_REQUESTS.discard(world_id)

def _region_build_stage_usage(manifest: dict) -> dict[str, dict[str, int | float]]:
    """Aggregate durable Region model usage without mixing Stage 1 and Stage 2."""
    stage_names = {
        "stage1_extraction": "stage1",
        "stage2_assessment": "stage2",
    }
    totals: dict[str, dict[str, int | float]] = {
        stage: {
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "elapsed_s": 0.0,
        }
        for stage in stage_names.values()
    }
    for call in manifest.get("calls") or []:
        if not isinstance(call, dict):
            continue
        stage = stage_names.get(str(call.get("stage") or ""))
        if not stage:
            continue
        prompt = int(call.get("prompt_tokens") or 0)
        completion = int(call.get("completion_tokens") or 0)
        item = totals[stage]
        item["requests"] = int(item["requests"]) + 1
        item["prompt_tokens"] = int(item["prompt_tokens"]) + prompt
        item["completion_tokens"] = int(item["completion_tokens"]) + completion
        item["total_tokens"] = int(item["total_tokens"]) + prompt + completion
        item["elapsed_s"] = float(item["elapsed_s"]) + float(call.get("elapsed_s") or 0.0)
    for item in totals.values():
        item["elapsed_s"] = round(float(item["elapsed_s"]), 3)
    return totals

def _region_stage2_expected_rule_ids(rules_asset: dict) -> set[str]:
    """Return only Stage-1 rules that must receive a Stage-2 assessment."""
    return {
        str(row.get("rule_id") or "")
        for row in rules_asset.get("rules") or []
        if isinstance(row, dict)
        and row.get("extraction_status") == "supported"
        and str(row.get("rule_id") or "")
    }

def _service_detail_jobs_root() -> Path:
    return PIPELINE_ROOT / "outputs" / "logs" / "vis" / "service_detail_jobs"

def _service_detail_job_snapshot(job: dict) -> dict:
    return {
        key: job.get(key)
        for key in (
            "job_id", "status", "world_id", "city", "city_label", "active_version",
            "place_count", "website_place_count", "estimated_request_minimum",
            "estimated_request_maximum", "started_at", "updated_at", "message",
            "phase", "progress_percent", "processed_places",
            "processed_website_places", "processed_without_website_places",
            "without_website_count", "records", "website_requests", "status_counts",
            "collection_mode", "checkpoint_processed_places", "checkpoint_website_requests",
        )
    }

def _persist_service_detail_job(job: dict) -> None:
    path = Path(job["_state_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {
        **_service_detail_job_snapshot(job),
        "diagnostic_error": str(job.get("diagnostic_error") or ""),
    })

def _update_service_detail_job(job: dict, **updates) -> None:
    with _SERVICE_DETAIL_JOB_LOCK:
        job.update(updates)
        job["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _persist_service_detail_job(job)

def _run_service_detail_job(job: dict) -> None:
    _update_service_detail_job(
        job,
        status="running",
        phase="preparing",
        message="正在准备当前城市版本的服务详情。",
    )

    phase_messages = {
        "collecting": "正在读取地点官网并整理服务详情。",
        "materializing": "网站采集已完成，正在生成本地服务资源。",
        "registering": "本地服务资源已生成，正在注册到当前城市版本。",
    }

    def report_progress(progress: dict) -> None:
        updates = {
            key: progress[key]
            for key in (
                "phase", "place_count", "processed_places", "website_place_count",
                "processed_website_places", "without_website_count",
                "processed_without_website_places", "website_requests", "status_counts",
            )
            if key in progress
        }
        total = int(updates.get("place_count", job.get("place_count") or 0))
        processed = int(updates.get("processed_places", job.get("processed_places") or 0))
        updates["progress_percent"] = min(100, round(processed * 100 / total)) if total else 0
        phase = str(updates.get("phase") or job.get("phase") or "")
        if phase in phase_messages:
            updates["message"] = phase_messages[phase]
        _update_service_detail_job(job, **updates)

    try:
        result = materialize_city_service_detail_snapshots(
            str(job["world_id"]),
            str(job["city"]),
            resume=str(job.get("collection_mode") or "resume") == "resume",
            retry_failures=False,
            expected_city_version=int(job["active_version"]),
            expected_map_world_sha256=str(job["map_world_sha256"]),
            progress_callback=report_progress,
        )
        materialized = next(
            (item for item in result.get("materialized") or []
             if str(item.get("city_id") or "") == str(job["city"])),
            {},
        )
        if materialized.get("status") != "available":
            raise RuntimeError("Service-detail construction did not produce a registered city artifact")
        cache_path = PIPELINE_ROOT / str(materialized.get("cache_path") or "")
        cache = read_json(cache_path) if cache_path.is_file() else {}
        summary = cache.get("collection_summary") or {}
        _update_service_detail_job(
            job,
            status="completed",
            phase="completed",
            progress_percent=100,
            message="服务详情构建完成。",
            records=int(summary.get("records") or 0),
            website_requests=int(summary.get("website_requests") or 0),
            status_counts=dict(summary.get("status_counts") or {}),
        )
    except Exception as exc:
        _update_service_detail_job(
            job,
            status="failed",
            phase="failed",
            message="服务详情构建失败。请查看服务端诊断日志后重试。",
            diagnostic_error=f"{type(exc).__name__}: {exc}",
        )

class GenerationHTTP(WorkbenchHTTP):

    def _json(self, payload, status=200):
        send(self.handler, payload, status)

    def handle(self, method, path):
        if path == '/api/generate/status':
            job_id = self.params.get('job_id', '')
            if job_id and (not re.fullmatch('[A-Za-z0-9_-]+', job_id)):
                self._error('Invalid job identifier.', 400)
                return
            job = _load_generation_job(job_id)
            if job and job.get('output_dir'):
                job['retry_batch_available'] = (Path(job['output_dir']) / 'batch_metadata.json').is_file()
            self._json({'job': job})
        elif path == '/api/route-planning/resources':
            self._route_planning_resources(self.params.get('city', ''))
        elif path == '/api/route-planning/cost-preflight':
            self._route_planning_cost_preflight()
        elif path == '/api/safety/route-resources':
            self._safety_route_resources(self.params.get('city', ''))
        elif path == '/api/safety/cost-preflight':
            self._safety_cost_preflight()
        elif path == '/api/multi-entity/route-resources':
            self._multi_entity_route_resources(self.params.get('city', ''))
        elif path == '/api/multi-entity/cost-preflight':
            self._multi_entity_cost_preflight()
        elif path == '/api/generate':
            self._handle_generate()
        elif path == '/api/itinerary/route-resources':
            self._itinerary_route_resources(self.params.get('city', ''))
        elif path == '/api/itinerary/cost-preflight':
            self._itinerary_cost_preflight()
        elif path == '/api/region/route-resources':
            self._region_route_resources(self.params.get("city", ""))
        elif path == '/api/region/cost-preflight':
            self._region_cost_preflight()
        elif path == '/api/region/service-detail/preflight':
            self._region_service_detail_preflight(self.params.get("city", ""))
        elif path == '/api/region/service-detail/status':
            self._region_service_detail_status(self.params.get("job_id", ""), self.params.get("city", ""))
        elif path == '/api/region/service-detail/start':
            self._start_region_service_detail_build()
        elif path == '/api/region/rule-cities':
            self._serve_region_rule_cities()
        elif path == '/api/region/rules':
            self._serve_region_rules(self.params.get("city", ""))
        elif path == '/api/region/rules/update':
            self._update_region_rule()
        elif path == '/api/region/build/config':
            self._serve_region_build_config()
        elif path == '/api/region/build/status':
            self._serve_region_build_status(self.params.get("city", ""))
        elif path == '/api/region/build/start':
            self._start_region_build()
        elif path == '/api/region/build/cancel':
            self._cancel_region_build()
        elif path == '/api/generate/cancel':
            self._cancel_generate()

    def _route_planning_resources(self, city: str) -> None:
        world_id = self._current_browsing_world_id()
        city = str(city or "").strip()
        cities = {item["city_id"]: item for item in self._current_world_cities()}
        if city not in cities:
            self._error("请选择当前世界中的城市。", 400)
            return
        version = int(cities[city]["active_version"])
        route_path = resolve_world_route_cache(world_id, city, version)
        cache_payload = read_json(route_path) if route_path and route_path.is_file() else {}
        cache_count = int(cache_payload.get("cached_pair_count") or len(cache_payload.get("cache") or []))
        pool_path = (route_path.parent / "route_poi_pool.json") if route_path else (
            PIPELINE_ROOT / "outputs" / "worlds" / world_id / "artifacts" / "routes" / city / f"v{version}" / "route_poi_pool.json"
        )
        pool_payload = read_json(pool_path) if pool_path.is_file() else {}
        settings_path = pool_path.parent / "route_generation_settings.json"
        settings_payload = read_json(settings_path) if settings_path.is_file() else {}
        pool_size = _route_cache_pool_size(cache_payload, pool_payload, self._route_city_world(world_id, city))
        configured_max = int(settings_payload.get("pool_max") or pool_payload.get("max_pois") or self._route_config_value("route_pool_max_pois", 50))
        diffusion_start = int(settings_payload.get("diffusion_start") or self._route_config_value("route_pool_diffusion_start", 20))
        characters = self._route_city_characters(world_id, city)
        character_count = len([
            item for item in characters.get("characters") or []
            if isinstance(item, dict) and item.get("character_id")
        ])
        self._json({
            "ok": True,
            "world_id": world_id,
            "city": city,
            "city_label": cities[city]["label"],
            "active_version": version,
            "pool_size": pool_size,
            "pool_max": configured_max,
            "diffusion_start": diffusion_start,
            "character_count": character_count,
            "cached_route_facts": cache_count,
            "graph_mode": str(pool_payload.get("graph_mode") or ("legacy_sparse" if cache_count else "dense")),
        })


    def _route_config_value(self, key: str, default: int) -> int:
        config_path = PIPELINE_ROOT / "config.yaml"
        if not config_path.is_file():
            return default
        try:
            return int(PipelineConfig(config_path).get(f"route_planning.{key}", default))
        except (OSError, ValueError, TypeError, KeyError):
            return default


    def _route_city_world(self, world_id: str, city: str) -> dict:
        registry_path = PIPELINE_ROOT / "outputs" / "worlds" / world_id / "cities" / f"{city}.json"
        if not registry_path.is_file():
            return {}
        try:
            record = read_json(registry_path)
            relative = str((record.get("artifacts") or {}).get("map_world", {}).get("path") or "")
            world_path = PIPELINE_ROOT / relative
            return read_json(world_path) if relative and world_path.is_file() else {}
        except (OSError, ValueError, TypeError):
            return {}


    def _route_city_characters(self, world_id: str, city: str) -> dict:
        registry_path = PIPELINE_ROOT / "outputs" / "worlds" / world_id / "cities" / f"{city}.json"
        if not registry_path.is_file():
            return {}
        try:
            record = read_json(registry_path)
            artifact = (record.get("artifacts") or {}).get("characters") or {}
            relative = str(artifact.get("path") or "") if artifact.get("status") == "available" else ""
            characters_path = PIPELINE_ROOT / relative
            return read_json(characters_path) if relative and characters_path.is_file() else {}
        except (OSError, ValueError, TypeError):
            return {}


    def _route_planning_cost_preflight(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        city = str(payload.get("city") or "").strip()
        try:
            count = int(payload.get("count") or 0)
            diffusion_start = int(payload.get("diffusion_start"))
            pool_max = int(payload.get("pool_max"))
        except (TypeError, ValueError):
            self._error("路线池参数必须是整数。", 400)
            return
        if count < 1 or diffusion_start < 1 or pool_max < 2 or diffusion_start >= pool_max:
            self._error("扩散阶段起点必须小于路线池上限。", 400)
            return
        world_id = self._current_browsing_world_id()
        cities = {item["city_id"]: item for item in self._current_world_cities()}
        if city not in cities:
            self._error("请选择当前世界中的城市。", 400)
            return
        version = int(cities[city]["active_version"])
        route_path = resolve_world_route_cache(world_id, city, version)
        route_payload = read_json(route_path) if route_path and route_path.is_file() else {}
        cached_route_facts = int(route_payload.get("cached_pair_count") or len(route_payload.get("cache") or []))
        pool_path = route_path.parent / "route_poi_pool.json" if route_path else None
        pool_payload = read_json(pool_path) if pool_path and pool_path.is_file() else {}
        pool_size = _route_cache_pool_size(route_payload, pool_payload, self._route_city_world(world_id, city))
        world = self._route_city_world(world_id, city)
        world_place_ids = {
            str(place.get("place_id") or "") for place in world.get("places") or []
            if isinstance(place, dict) and place.get("place_id")
        }
        initial_pool_ids = [
            str(place_id) for place_id in pool_payload.get("place_ids") or []
            if str(place_id) in world_place_ids
        ]
        if not initial_pool_ids and pool_size:
            coordinate_to_id = {
                (round(float(place["location"]["lat"]), 6), round(float(place["location"]["lng"]), 6)): str(place.get("place_id") or "")
                for place in world.get("places") or []
                if isinstance(place.get("location"), dict)
                and place["location"].get("lat") is not None and place["location"].get("lng") is not None
            }
            legacy_ids: set[str] = set()
            for entry in route_payload.get("cache") or []:
                for prefix in ("origin", "dest"):
                    place_id = str(entry.get(f"{prefix}_place_id") or "")
                    if place_id in world_place_ids:
                        legacy_ids.add(place_id)
                        continue
                    try:
                        mapped = coordinate_to_id.get((
                            round(float(entry.get(f"{prefix}_lat")), 6),
                            round(float(entry.get(f"{prefix}_lng")), 6),
                        ))
                    except (TypeError, ValueError):
                        mapped = None
                    if mapped:
                        legacy_ids.add(mapped)
            initial_pool_ids = sorted(legacy_ids)

        requested_mode = str(payload.get("mode") or "append")
        requested_batch = str(payload.get("batch") or "")
        preview_batch_dir: Path | None = None
        if requested_mode == "append":
            available = _list_batches(self.benchmarks_root, "route_planning", city, world_id)
            known = {entry["name"] for entry in available["batches"]}
            resolved_batch = requested_batch if requested_batch in known else available.get("current", "")
            if not resolved_batch and available["batches"]:
                resolved_batch = available["batches"][0]["name"]
            if resolved_batch:
                preview_batch_dir = managed_batch_dir(
                    self.benchmarks_root, "route_planning", world_id, city, resolved_batch,
                )
        metadata_path = preview_batch_dir / "batch_metadata.json" if preview_batch_dir else None
        metadata = read_json(metadata_path) if metadata_path and metadata_path.is_file() else {}
        batch_seed = int(metadata.get("batch_seed") or 0) or (secrets.randbits(63) or 1)
        task_index = next_task_index(preview_batch_dir) if preview_batch_dir else 1
        graph_mode = str(pool_payload.get("graph_mode") or ("legacy_sparse" if initial_pool_ids else "dense"))
        preview = build_route_cost_preview(
            world=world, cache_payload=route_payload, initial_pool_ids=initial_pool_ids,
            graph_mode=graph_mode, count=count, diffusion_start=diffusion_start,
            pool_max=pool_max, batch_seed=batch_seed, world_id=world_id, city_id=city,
            next_task_index=task_index,
        )
        route_state_hash = hashlib.sha256(json.dumps(
            {"route": route_payload, "pool": pool_payload},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        preflight_id = f"route_cost_{uuid.uuid4().hex}"
        result = {
            "ok": True,
            "preflight_id": preflight_id,
            "world_id": world_id,
            "city": city,
            "city_label": cities[city]["label"],
            "active_version": version,
            "count": count,
            "mode": requested_mode,
            "batch": requested_batch,
            "diffusion_start": diffusion_start,
            "pool_max": pool_max,
            "pool_size": pool_size,
            "cached_route_facts": cached_route_facts,
            "graph_mode": graph_mode,
            "batch_seed": batch_seed,
            "next_task_index": task_index,
            "route_state_hash": route_state_hash,
            "cost_preview": preview,
            "estimated_new_pois": preview["maximum"]["new_pois"],
            "estimated_api_requests": preview["maximum"]["requests"],
            "sku": "Compute Routes Essentials",
            "unit_price_usd": preview["unit_price_usd"],
            "estimated_cost_usd": preview["maximum"]["cost_usd"],
            "route_cost_actual": _route_cost_actual(
                route_path.parent / "route_cost_events.jsonl" if route_path else None,
            ),
        }
        _ROUTE_COST_PREFLIGHTS[preflight_id] = dict(result)
        self._json(result)


    def _safety_bound_route(self, world_id: str, city: str) -> Path | None:
        from task_module.task_types.safety_planning.generation_inputs import resolve_assets

        try:
            return Path(resolve_assets(PIPELINE_ROOT, world_id, city)["route_cache"])
        except (KeyError, OSError, ValueError, StopIteration) as exc:
            self._error(f"Safety frozen assets are unavailable: {exc}", 409)
            return None

    def _safety_route_resources(self, city: str) -> None:
        world_id = self._current_browsing_world_id()
        cities = {item["city_id"]: item for item in self._current_world_cities()}
        if city not in cities:
            self._error("请选择当前世界中的城市。", 400)
            return
        version = int(cities[city]["active_version"])
        route_path = self._safety_bound_route(world_id, city)
        if route_path is None:
            return
        route_payload = _multi_entity_effective_route_payload(route_path)
        pool_path = route_path.parent / "route_poi_pool.json" if route_path else None
        pool_payload = read_json(pool_path) if pool_path and pool_path.is_file() else {}
        world = self._route_city_world(world_id, city)
        characters = self._route_city_characters(world_id, city)
        routing_ids = safety_routing_place_ids(world, pool_payload, route_payload)
        self._json({
            "ok": True, "world_id": world_id, "city": city,
            "city_label": cities[city]["label"], "active_version": version,
            "pool_size": len(routing_ids),
            "character_count": len(characters.get("characters") or []),
            "cached_route_facts": int(route_payload.get("cached_pair_count") or 0),
        })

    def _cost_preview_batch_context(
        self, task_type: str, city: str, world_id: str, requested_mode: str, requested_batch: str,
    ) -> tuple[int, int]:
        preview_batch_dir: Path | None = None
        if requested_mode == "append":
            available = _list_batches(self.benchmarks_root, task_type, city, world_id)
            resolved_batch = _resolve_generation_append_batch(
                self.benchmarks_root, task_type, city, world_id, requested_batch, available,
            )
            if resolved_batch:
                preview_batch_dir = managed_batch_dir(
                    self.benchmarks_root, task_type, world_id, city, resolved_batch,
                )
        metadata_path = preview_batch_dir / "batch_metadata.json" if preview_batch_dir else None
        metadata = read_json(metadata_path) if metadata_path and metadata_path.is_file() else {}
        seed = int(metadata.get("batch_seed") or 0) or (secrets.randbits(63) or 1)
        return seed, next_task_index(preview_batch_dir) if preview_batch_dir else 1

    def _safety_cost_preflight(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        city = str(payload.get("city") or "").strip()
        try:
            count = int(payload.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count < 1:
            self._error("任务数量必须是正整数。", 400)
            return
        world_id = self._current_browsing_world_id()
        cities = {item["city_id"]: item for item in self._current_world_cities()}
        if city not in cities:
            self._error("请选择当前世界中的城市。", 400)
            return
        version = int(cities[city]["active_version"])
        route_path = self._safety_bound_route(world_id, city)
        if route_path is None:
            return
        route_payload = _multi_entity_effective_route_payload(route_path)
        pool_path = route_path.parent / "route_poi_pool.json" if route_path else None
        pool_payload = read_json(pool_path) if pool_path and pool_path.is_file() else {}
        world = self._route_city_world(world_id, city)
        characters = self._route_city_characters(world_id, city)
        requested_mode = str(payload.get("mode") or "append")
        requested_batch = str(payload.get("batch") or "")
        batch_seed, task_index = self._cost_preview_batch_context(
            "safety_planning", city, world_id, requested_mode, requested_batch,
        )
        try:
            preview = build_safety_cost_preview(
                world=world, characters=characters, cache_payload=route_payload,
                route_pool_payload=pool_payload, count=count, batch_seed=batch_seed,
                world_id=world_id, city_id=city, next_task_index=task_index,
            )
        except ValueError as exc:
            self._error(str(exc), 409)
            return
        state_hash = hashlib.sha256(json.dumps(
            {"route": route_payload, "pool": pool_payload, "world": world, "characters": characters},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        preflight_id = f"safety_cost_{uuid.uuid4().hex}"
        result = {
            "ok": True, "preflight_id": preflight_id, "world_id": world_id,
            "city": city, "city_label": cities[city]["label"], "active_version": version,
            "count": count, "mode": requested_mode, "batch": requested_batch,
            "pool_size": len(safety_routing_place_ids(world, pool_payload, route_payload)),
            "character_count": len(characters.get("characters") or []),
            "cached_route_facts": int(route_payload.get("cached_pair_count") or 0),
            "batch_seed": batch_seed, "next_task_index": task_index,
            "resource_state_hash": state_hash, "cost_preview": preview,
            "sku": "Compute Routes Essentials", "unit_price_usd": preview["unit_price_usd"],
            "route_cost_actual": _route_cost_actual(
                route_path.parent / "route_cost_events.jsonl" if route_path else None,
            ),
        }
        _SAFETY_COST_PREFLIGHTS[preflight_id] = dict(result)
        self._json(result)

    def _multi_entity_route_resources(self, city: str) -> None:
        world_id = self._current_browsing_world_id()
        city = str(city or "").strip()
        cities = {item["city_id"]: item for item in self._current_world_cities()}
        if city not in cities:
            self._error("请选择当前世界中的城市。", 400)
            return
        version = int(cities[city]["active_version"])
        route_path = resolve_world_route_cache(world_id, city, version)
        route_payload = _multi_entity_effective_route_payload(route_path)
        pool_path = route_path.parent / "route_poi_pool.json" if route_path else None
        pool_payload = read_json(pool_path) if pool_path and pool_path.is_file() else {}
        world = self._route_city_world(world_id, city)
        routing_ids = _multi_entity_routing_place_ids(route_payload, pool_payload, world)
        characters = self._route_city_characters(world_id, city)
        character_ids = {
            str(item.get("character_id") or "") for item in characters.get("characters") or []
            if isinstance(item, dict) and item.get("character_id")
        }
        entries = [entry for entry in route_payload.get("cache") or [] if isinstance(entry, dict)]
        self._json({
            "ok": True, "world_id": world_id, "city": city,
            "city_label": cities[city]["label"], "active_version": version,
            "pool_size": len(routing_ids),
            "character_count": len(character_ids),
            "cached_route_facts": int(route_payload.get("cached_pair_count") or len(entries)),
            "character_route_facts": _character_route_fact_count(entries, character_ids),
        })


    def _multi_entity_cost_preflight(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        city = str(payload.get("city") or "").strip()
        try:
            count = int(payload.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count < 1:
            self._error("任务数量必须是正整数。", 400)
            return
        world_id = self._current_browsing_world_id()
        cities = {item["city_id"]: item for item in self._current_world_cities()}
        if city not in cities:
            self._error("请选择当前世界中的城市。", 400)
            return
        version = int(cities[city]["active_version"])
        route_path = resolve_world_route_cache(world_id, city, version)
        route_payload = _multi_entity_effective_route_payload(route_path)
        pool_path = route_path.parent / "route_poi_pool.json" if route_path else None
        pool_payload = read_json(pool_path) if pool_path and pool_path.is_file() else {}
        world = self._route_city_world(world_id, city)
        characters = self._route_city_characters(world_id, city)
        routing_ids = _multi_entity_routing_place_ids(route_payload, pool_payload, world)
        if not routing_ids:
            self._error("当前城市没有可供 Multi-Entity 使用的 Routing POI池。", 409)
            return
        if len(characters.get("characters") or []) < 2:
            self._error("当前城市至少需要两名可用人物。", 409)
            return
        requested_mode = str(payload.get("mode") or "append")
        requested_batch = str(payload.get("batch") or "")
        preview_batch_dir: Path | None = None
        if requested_mode == "append":
            available = _list_batches(self.benchmarks_root, "multi_entity", city, world_id)
            known = {entry["name"] for entry in available["batches"]}
            resolved_batch = requested_batch if requested_batch in known else available.get("current", "")
            if resolved_batch:
                preview_batch_dir = managed_batch_dir(
                    self.benchmarks_root, "multi_entity", world_id, city, resolved_batch,
                )
        metadata_path = preview_batch_dir / "batch_metadata.json" if preview_batch_dir else None
        metadata = read_json(metadata_path) if metadata_path and metadata_path.is_file() else {}
        batch_seed = int(metadata.get("batch_seed") or 0) or (secrets.randbits(63) or 1)
        task_index = next_task_index(preview_batch_dir) if preview_batch_dir else 1
        try:
            preview = build_multi_entity_cost_preview(
                world=world, characters=characters, cache_payload=route_payload,
                routing_place_ids=routing_ids, count=count, batch_seed=batch_seed,
                world_id=world_id, city_id=city, next_task_index=task_index,
            )
        except ValueError as exc:
            self._error(str(exc), 409)
            return
        resource_state_hash = hashlib.sha256(json.dumps(
            {"route": route_payload, "pool": pool_payload, "characters": characters},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        preflight_id = f"multi_entity_cost_{uuid.uuid4().hex}"
        entries = [entry for entry in route_payload.get("cache") or [] if isinstance(entry, dict)]
        character_ids = {
            str(item.get("character_id") or "") for item in characters.get("characters") or []
            if isinstance(item, dict)
        }
        result = {
            "ok": True, "preflight_id": preflight_id, "world_id": world_id,
            "city": city, "city_label": cities[city]["label"], "active_version": version,
            "count": count, "mode": requested_mode, "batch": requested_batch,
            "pool_size": len(routing_ids), "character_count": len(character_ids),
            "cached_route_facts": int(route_payload.get("cached_pair_count") or len(entries)),
            "character_route_facts": _character_route_fact_count(entries, character_ids),
            "batch_seed": batch_seed, "next_task_index": task_index,
            "resource_state_hash": resource_state_hash, "cost_preview": preview,
            "sku": "Compute Routes Essentials", "unit_price_usd": preview["unit_price_usd"],
            "route_cost_actual": _route_cost_actual(
                route_path.parent / "route_cost_events.jsonl" if route_path else None,
            ),
        }
        _MULTI_ENTITY_COST_PREFLIGHTS[preflight_id] = dict(result)
        self._json(result)


    def _itinerary_route_resources(self, city: str) -> None:
        world_id = self._current_browsing_world_id()
        city = str(city or "").strip()
        cities = {item["city_id"]: item for item in self._current_world_cities()}
        if city not in cities:
            self._error("请选择当前世界中的城市。", 400)
            return
        version = int(cities[city]["active_version"])
        route_path = resolve_world_route_cache(world_id, city, version)
        route_payload = _multi_entity_effective_route_payload(route_path)
        pool_path = route_path.parent / "route_poi_pool.json" if route_path else None
        pool_payload = read_json(pool_path) if pool_path and pool_path.is_file() else {}
        world = self._route_city_world(world_id, city)
        routing_ids = itinerary_routing_place_ids(world, pool_payload, route_payload)
        characters = self._route_city_characters(world_id, city)
        character_count = len([
            item for item in characters.get("characters") or []
            if isinstance(item, dict) and item.get("character_id")
        ])
        entries = [entry for entry in route_payload.get("cache") or [] if isinstance(entry, dict)]
        self._json({
            "ok": True, "world_id": world_id, "city": city,
            "city_label": cities[city]["label"], "active_version": version,
            "pool_size": len(routing_ids), "character_count": character_count,
            "cached_route_facts": int(route_payload.get("cached_pair_count") or len(entries)),
        })

    def _itinerary_cost_preflight(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        city = str(payload.get("city") or "").strip()
        try:
            count = int(payload.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count < 1:
            self._error("任务数量必须是正整数。", 400)
            return
        world_id = self._current_browsing_world_id()
        cities = {item["city_id"]: item for item in self._current_world_cities()}
        if city not in cities:
            self._error("请选择当前世界中的城市。", 400)
            return
        version = int(cities[city]["active_version"])
        route_path = resolve_world_route_cache(world_id, city, version)
        route_payload = _multi_entity_effective_route_payload(route_path)
        pool_path = route_path.parent / "route_poi_pool.json" if route_path else None
        pool_payload = read_json(pool_path) if pool_path and pool_path.is_file() else {}
        world = self._route_city_world(world_id, city)
        characters = self._route_city_characters(world_id, city)
        routing_ids = itinerary_routing_place_ids(world, pool_payload, route_payload)
        if not routing_ids:
            self._error("当前城市没有可供 Itinerary 使用的 Routing POI池。", 409)
            return
        if not characters.get("characters"):
            self._error("当前城市至少需要一名可用人物。", 409)
            return
        requested_mode = str(payload.get("mode") or "append")
        requested_batch = str(payload.get("batch") or "")
        preview_batch_dir: Path | None = None
        if requested_mode == "append":
            available = _list_batches(self.benchmarks_root, "itinerary", city, world_id)
            known = {entry["name"] for entry in available["batches"]}
            resolved_batch = requested_batch if requested_batch in known else available.get("current", "")
            if resolved_batch:
                preview_batch_dir = managed_batch_dir(
                    self.benchmarks_root, "itinerary", world_id, city, resolved_batch,
                )
        metadata_path = preview_batch_dir / "batch_metadata.json" if preview_batch_dir else None
        metadata = read_json(metadata_path) if metadata_path and metadata_path.is_file() else {}
        batch_seed = int(metadata.get("batch_seed") or 0) or (secrets.randbits(63) or 1)
        task_index = next_task_index(preview_batch_dir) if preview_batch_dir else 1
        try:
            preview = build_itinerary_cost_preview(
                world=world, characters=characters, cache_payload=route_payload,
                route_pool_payload=pool_payload, count=count, batch_seed=batch_seed,
                world_id=world_id, city_id=city, next_task_index=task_index,
            )
        except ValueError as exc:
            self._error(str(exc), 409)
            return
        resource_state_hash = hashlib.sha256(json.dumps(
            {"route": route_payload, "pool": pool_payload,
             "world": world, "characters": characters},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        preflight_id = f"itinerary_cost_{uuid.uuid4().hex}"
        result = {
            "ok": True, "preflight_id": preflight_id, "world_id": world_id,
            "city": city, "city_label": cities[city]["label"], "active_version": version,
            "count": count, "mode": requested_mode, "batch": requested_batch,
            "pool_size": len(routing_ids),
            "character_count": len(characters.get("characters") or []),
            "cached_route_facts": int(route_payload.get("cached_pair_count") or len(route_payload.get("cache") or [])),
            "batch_seed": batch_seed, "next_task_index": task_index,
            "resource_state_hash": resource_state_hash, "cost_preview": preview,
            "sku": "Compute Routes Essentials", "unit_price_usd": preview["unit_price_usd"],
            "route_cost_actual": _route_cost_actual(
                route_path.parent / "route_cost_events.jsonl" if route_path else None,
            ),
        }
        _ITINERARY_COST_PREFLIGHTS[preflight_id] = dict(result)
        self._json(result)

    def _region_route_resources(self, city: str) -> None:
        world_id = self._current_browsing_world_id()
        cities = {item["city_id"]: item for item in self._current_world_cities()}
        if city not in cities:
            self._error("请选择当前世界中的城市。", 400)
            return
        version = int(cities[city]["active_version"])
        artifacts = self._active_city_artifacts(world_id, city, version)
        service_path = self._registered_artifact_path(artifacts, "service_detail_cache")
        pipeline_root = self._region_pipeline_root(city)
        issues = []
        if service_path is None:
            issues.append({
                "code": "service_detail_missing",
                "message": "当前城市版本尚未构建服务详情。Region 任务需要先完成该资源。",
                "action": "build_service_detail",
            })
        if pipeline_root is None:
            issues.append({
                "code": "region_rules_missing",
                "message": "当前城市尚未完成 Region 地域规则准备。",
            })
        route_path = resolve_world_route_cache(world_id, city, version)
        route_payload = _multi_entity_effective_route_payload(route_path)
        pool_path = route_path.parent / "route_poi_pool.json" if route_path else None
        pool_payload = read_json(pool_path) if pool_path and pool_path.is_file() else {}
        world = self._route_city_world(world_id, city)
        characters = self._route_city_characters(world_id, city)
        routing_ids = region_routing_place_ids(world, pool_payload, route_payload)
        self._json({
            "ok": True, "world_id": world_id, "city": city,
            "city_label": cities[city]["label"], "active_version": version,
            "pool_size": len(routing_ids),
            "character_count": len(characters.get("characters") or []),
            "cached_route_facts": int(route_payload.get("cached_pair_count") or 0),
            "service_detail_ready": service_path is not None,
            "region_rules_ready": pipeline_root is not None,
            "resource_issues": issues,
            "error": "；".join(str(item["message"]) for item in issues),
        })

    def _region_service_detail_preflight(self, city: str) -> None:
        world_id = self._current_browsing_world_id()
        cities = {item["city_id"]: item for item in self._current_world_cities()}
        if city not in cities:
            self._error("请选择当前世界中的城市。", 400)
            return
        version = int(cities[city]["active_version"])
        artifacts = self._active_city_artifacts(world_id, city, version)
        if self._registered_artifact_path(artifacts, "service_detail_cache") is not None:
            self._error("当前城市版本的服务详情已经就绪，无需重复构建。", 409)
            return
        map_path = self._registered_artifact_path(artifacts, "map_world")
        if map_path is None:
            self._error("当前城市版本的地图资源无法读取，暂时不能构建服务详情。", 409)
            return
        world = read_json(map_path)
        places = [item for item in world.get("places") or [] if isinstance(item, dict)]
        existing_path = map_path.parent / "service_detail_cache.json"
        existing = read_json(existing_path) if existing_path.is_file() else {}
        checkpoint_path = map_path.parent / "service_detail_cache.checkpoint.jsonl"
        checkpoint = read_service_detail_checkpoint(
            checkpoint_path, world, map_world_sha256=sha256_file(map_path),
        )
        existing_by_identity = {}
        for record in [*(existing.get("records") or []), *(checkpoint.get("records") or [])]:
            identity = public_identity(record) if isinstance(record, dict) else None
            if identity is not None:
                existing_by_identity[identity_key(identity)] = record
        website_place_count = 0
        reusable_website_records = 0
        websites_to_fetch = 0
        for place in places:
            uri = website_uri(place)
            if not uri:
                continue
            website_place_count += 1
            identity = public_identity(place)
            prior = existing_by_identity.get(identity_key(identity)) if identity is not None else None
            if prior is not None and str(prior.get("website_uri") or "") == uri:
                reusable_website_records += 1
            else:
                websites_to_fetch += 1
        map_world_sha256 = sha256_file(map_path)
        preflight_id = f"service_detail_{uuid.uuid4().hex}"
        preflight = {
            "ok": True,
            "preflight_id": preflight_id,
            "world_id": world_id,
            "city": city,
            "city_label": cities[city]["label"],
            "active_version": version,
            "map_world_sha256": map_world_sha256,
            "place_count": len(places),
            "website_place_count": website_place_count,
            "without_website_count": len(places) - website_place_count,
            "reusable_website_records": reusable_website_records,
            "checkpoint_available": bool(checkpoint.get("valid") and checkpoint.get("records")),
            "checkpoint_processed_places": len(checkpoint.get("records") or []),
            "checkpoint_website_requests": int(checkpoint.get("website_requests") or 0),
            "estimated_request_minimum": websites_to_fetch,
            "estimated_request_maximum": websites_to_fetch * 4,
            "request_kind": "普通网站 HTTP 请求（不调用 Google API）",
        }
        _SERVICE_DETAIL_PREFLIGHTS[preflight_id] = dict(preflight)
        self._json(preflight)

    def _region_service_detail_status(self, job_id: str, city: str) -> None:
        with _SERVICE_DETAIL_JOB_LOCK:
            job = _SERVICE_DETAIL_JOBS.get(job_id) if job_id else next(
                (item for item in _SERVICE_DETAIL_JOBS.values()
                 if str(item.get("world_id") or "") == self._current_browsing_world_id()
                 and str(item.get("city") or "") == str(city or "")),
                None,
            )
            self._json({"ok": True, "job": _service_detail_job_snapshot(job) if job else None})

    def _start_region_service_detail_build(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        preflight_id = str(payload.get("preflight_id") or "")
        collection_mode = str(payload.get("collection_mode") or "resume")
        if collection_mode not in {"resume", "restart"}:
            self._error("请选择续采集或重新采集。", 400)
            return
        preflight = _SERVICE_DETAIL_PREFLIGHTS.get(preflight_id)
        if not preflight:
            self._error("服务详情构建预估已失效，请重新查看。", 409)
            return
        world_id = self._current_browsing_world_id()
        city = str(preflight["city"])
        cities = {item["city_id"]: item for item in self._current_world_cities()}
        current = cities.get(city) or {}
        if (world_id != preflight["world_id"]
                or int(current.get("active_version") or 0) != int(preflight["active_version"])):
            self._error("当前城市版本已经变化，请重新查看服务详情构建信息。", 409)
            return
        artifacts = self._active_city_artifacts(world_id, city, int(preflight["active_version"]))
        map_path = self._registered_artifact_path(artifacts, "map_world")
        if map_path is None or sha256_file(map_path) != preflight["map_world_sha256"]:
            self._error("当前城市地图已经变化，请重新查看服务详情构建信息。", 409)
            return
        with _SERVICE_DETAIL_JOB_LOCK:
            active = next(
                (item for item in _SERVICE_DETAIL_JOBS.values()
                 if item.get("status") in {"queued", "running"}
                 and item.get("world_id") == world_id and item.get("city") == city),
                None,
            )
            if active:
                self._json({"ok": True, "job": _service_detail_job_snapshot(active)})
                return
            job_id = f"service_detail_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
            job = {
                **preflight,
                "job_id": job_id,
                "status": "queued",
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "message": "服务详情构建已进入后台队列。",
                "collection_mode": collection_mode,
                "phase": "queued",
                "progress_percent": 0,
                "processed_places": 0,
                "processed_website_places": 0,
                "processed_without_website_places": 0,
                "without_website_count": int(preflight.get("without_website_count") or 0),
                "website_requests": 0,
                "status_counts": {},
                "_state_path": str(_service_detail_jobs_root() / f"{job_id}.json"),
            }
            _SERVICE_DETAIL_JOBS[job_id] = job
            _persist_service_detail_job(job)
        threading.Thread(target=_run_service_detail_job, args=(job,), daemon=True).start()
        self._json({"ok": True, "job": _service_detail_job_snapshot(job)})

    def _region_cost_preflight(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        city = str(payload.get("city") or "").strip()
        try:
            count = int(payload.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count < 1:
            self._error("任务数量必须是正整数。", 400)
            return
        world_id = self._current_browsing_world_id()
        cities = {item["city_id"]: item for item in self._current_world_cities()}
        if city not in cities:
            self._error("请选择当前世界中的城市。", 400)
            return
        version = int(cities[city]["active_version"])
        artifacts = self._active_city_artifacts(world_id, city, version)
        service_path = self._registered_artifact_path(artifacts, "service_detail_cache")
        pipeline_root = self._region_pipeline_root(city)
        if service_path is None:
            self._error("当前城市版本尚未构建服务详情。请先完成该资源，再预估 Region 成本。", 409)
            return
        if pipeline_root is None:
            self._error("当前城市缺少完整且与当前世界绑定的 Region Stage 1/2 正式资产。", 409)
            return
        route_path = resolve_world_route_cache(world_id, city, version)
        route_payload = _multi_entity_effective_route_payload(route_path)
        pool_path = route_path.parent / "route_poi_pool.json" if route_path else None
        pool_payload = read_json(pool_path) if pool_path and pool_path.is_file() else {}
        world = self._route_city_world(world_id, city)
        characters = self._route_city_characters(world_id, city)
        rules_asset = read_json(pipeline_root / "regional_rules.json")
        stage2_asset = read_json(pipeline_root / "regional_rule_stage2_assessments.json")
        service_cache = read_json(service_path)
        requested_mode = str(payload.get("mode") or "append")
        requested_batch = str(payload.get("batch") or "")
        batch_seed, task_index = self._cost_preview_batch_context(
            "region_aware", city, world_id, requested_mode, requested_batch,
        )
        try:
            preview = build_region_cost_preview(
                world=world, characters=characters, cache_payload=route_payload,
                route_pool_payload=pool_payload, rules_asset=rules_asset,
                stage2_asset=stage2_asset, service_detail_cache=service_cache,
                count=count, batch_seed=batch_seed, world_id=world_id,
                city_id=city, next_task_index=task_index,
            )
        except ValueError as exc:
            self._error(str(exc), 409)
            return
        state_hash = hashlib.sha256(json.dumps(
            {"route": route_payload, "pool": pool_payload, "world": world,
             "characters": characters, "rules": rules_asset, "stage2": stage2_asset,
             "service_detail": service_cache},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        preflight_id = f"region_cost_{uuid.uuid4().hex}"
        result = {
            "ok": True, "preflight_id": preflight_id, "world_id": world_id,
            "city": city, "city_label": cities[city]["label"], "active_version": version,
            "count": count, "mode": requested_mode, "batch": requested_batch,
            "pool_size": len(region_routing_place_ids(world, pool_payload, route_payload)),
            "character_count": len(characters.get("characters") or []),
            "cached_route_facts": int(route_payload.get("cached_pair_count") or 0),
            "service_detail_ready": True, "region_rules_ready": True,
            "batch_seed": batch_seed, "next_task_index": task_index,
            "resource_state_hash": state_hash, "cost_preview": preview,
            "sku": "Compute Routes Essentials", "unit_price_usd": preview["unit_price_usd"],
            "route_cost_actual": _route_cost_actual(
                route_path.parent / "route_cost_events.jsonl" if route_path else None,
            ),
        }
        _REGION_COST_PREFLIGHTS[preflight_id] = dict(result)
        self._json(result)

    def _region_build_paths(self, city_id: str) -> tuple[Path, Path, Path]:
        cultural = PIPELINE_ROOT / "outputs" / "worlds" / self._current_browsing_world_id() / "artifacts" / "cultural" / city_id
        draft = cultural / "region_source_pipeline_build"
        return draft, cultural / "region_source_pipeline_formal", draft / "ui_build_state.json"

    def _region_active_world_path(self, city_id: str) -> tuple[Path, str]:
        world_id = self._current_browsing_world_id()
        record_path = PIPELINE_ROOT / "outputs" / "worlds" / world_id / "cities" / f"{city_id}.json"
        record = read_json(record_path)
        version = int(record.get("active_version") or 1)
        version_entry = next((row for row in record.get("city_versions") or [] if int(row.get("version") or 0) == version), None)
        artifacts = (version_entry or {}).get("artifacts") or record.get("artifacts") or {}
        reference = artifacts.get("map_world") or {}
        world_path = PIPELINE_ROOT / str(reference.get("path") or "")
        if reference.get("status") != "available" or not world_path.is_file():
            raise FileNotFoundError(f"当前城市 {city_id} 的 v{version} world.json 不可用。")
        return world_path, f"v{version}"

    def _serve_region_build_config(self) -> None:
        cities = []
        for city in self._current_world_cities():
            city_id = str(city.get("city_id") or "")
            if not city_id:
                continue
            draft, _, state_path = self._region_build_paths(city_id)
            state = read_json(state_path) if state_path.is_file() else {}
            cities.append({
                "city_id": city_id,
                "city_name": city.get("city_name") or city.get("label") or city_id,
                "active_version": city.get("active_version"),
                "built": self._region_pipeline_root(city_id) is not None,
                "has_draft": draft.is_dir(),
                "build_status": state.get("status", "not_started"),
            })
        config = PipelineConfig("config.yaml")
        text_models = _model_options()["generation"]
        vision_keys = {
            key for key, endpoint in (config.get("model_endpoints", {}) or {}).items()
            if endpoint.get("task_generation", False) and endpoint.get("supports_vision", False)
        }
        self._json({
            "cities": cities,
            "text_models": text_models,
            "vision_models": [row for row in text_models if row["endpoint_key"] in vision_keys],
            "default_vision_model": config.stage_model("vision_parse"),
        })

    def _serve_region_build_status(self, city_id: str) -> None:
        if not city_id:
            self._error("Missing city", 400)
            return
        draft, _, state_path = self._region_build_paths(city_id)
        state = read_json(state_path) if state_path.is_file() else {"city_id": city_id, "status": "not_started", "phase": "not_started"}
        manifest_path = draft / "run_manifest.json"
        manifest = read_json(manifest_path) if manifest_path.is_file() else {}
        state["stage_usage"] = _region_build_stage_usage(manifest)
        if state.get("phase") == "stage1_extraction":
            stage1_root = draft / "_work" / "stage1_extraction"
            cached = sum(
                1 for path in stage1_root.glob("*.json")
                if not path.name.endswith(".failed.json")
            ) if stage1_root.is_dir() else 0
            total = int(manifest.get("chunks") or state.get("stage1_total") or 0)
            state.update({
                "completed": cached, "total": total,
                "stage1_completed": cached, "stage1_total": total,
            })
        with _REGION_BUILD_LOCK:
            active = city_id in _REGION_BUILD_PROCESSES or city_id in _REGION_BUILD_JOBS
        active = active or _process_is_running(state.get("process_pid"))
        if state.get("status") in {"queued", "running", "retry_wait"} and not active:
            # A status GET must remain read-only. After a service restart the
            # durable file can still say running although its owner no longer
            # exists; expose the recoverable effective state without racing a
            # former worker for the same high-frequency status file.
            state.update({"status": "interrupted", "error": "审核服务重启或构建进程已中断，可继续当前阶段。"})
        retry_until = float(state.get("internal_retry_until") or 0)
        state["internal_retry_remaining"] = max(0, int(math.ceil(retry_until - time.time()))) if retry_until else 0
        model_retry_until = float(state.get("internal_model_retry_until") or 0)
        state["internal_model_retry_remaining"] = max(0, int(math.ceil(model_retry_until - time.time()))) if model_retry_until else 0
        rules_path = draft / "regional_rules.json"
        if rules_path.is_file():
            state["stage1_rule_count"] = len(read_json(rules_path).get("rules") or [])
        self._json({"build": state})

    def _start_region_build(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        text_model = str(payload.get("text_model") or payload.get("model") or "quality_llm")
        config = PipelineConfig("config.yaml")
        vision_model = str(payload.get("vision_model") or config.stage_model("vision_parse"))
        cities = {str(row.get("city_id") or ""): row for row in self._current_world_cities()}
        allowed_models = {row["endpoint_key"] for row in _model_options()["generation"]}
        if text_model not in allowed_models:
            self._error("Region 文本模型无效。", 400)
            return
        vision_endpoint = (config.get("model_endpoints", {}) or {}).get(vision_model) or {}
        if vision_model not in allowed_models or not vision_endpoint.get("supports_vision", False):
            self._error("Region 图像模型无效或不支持图片输入。", 400)
            return
        selections = payload.get("cities")
        if not isinstance(selections, list) or not selections:
            self._error("请至少选择一个目标城市。", 400)
            return
        world_id = self._current_browsing_world_id()
        with _REGION_BUILD_LOCK:
            if world_id in _REGION_BUILD_BATCHES:
                self._error("当前 World 已有 Region 城市队列正在运行。", 409)
                return
            _REGION_BUILD_CANCEL_REQUESTS.discard(world_id)
        jobs = []
        seen: set[str] = set()
        for selection in selections:
            if not isinstance(selection, dict):
                self._error("城市构建项格式无效。", 400)
                return
            city_id = str(selection.get("city_id") or "")
            if city_id not in cities or city_id in seen:
                self._error("目标城市无效或重复。", 400)
                return
            seen.add(city_id)
            if self._region_pipeline_root(city_id) is not None:
                self._error(f"{city_id} 已有完整正式 Region 资产；新建入口不会覆盖正式资产。", 409)
                return
            draft, formal, state_path = self._region_build_paths(city_id)
            draft.mkdir(parents=True, exist_ok=True)
            prior = read_json(state_path) if state_path.is_file() else {}
            if prior.get("status") in {"queued", "running", "retry_wait"} and _process_is_running(prior.get("process_pid")):
                self._error(f"{city_id} 的 Region 构建进程仍在运行，不能重复启动。", 409)
                return
            manifest_path = draft / "run_manifest.json"
            manifest = read_json(manifest_path) if manifest_path.is_file() else {}
            saved_progress = manifest.get("progress") if isinstance(manifest.get("progress"), dict) else {}
            saved_phase = str(saved_progress.get("stage") or "")
            saved_completed = int(saved_progress.get("completed") or 0)
            saved_total = int(saved_progress.get("total") or 0)
            stage1_cache_root = draft / "_work" / "stage1_extraction"
            cached_stage1 = sum(
                1 for path in stage1_cache_root.glob("*.json")
                if not path.name.endswith(".failed.json")
            ) if stage1_cache_root.is_dir() else 0
            stage1_total = int(manifest.get("chunks") or 0)
            legacy_evidence_path = (
                PIPELINE_ROOT / "outputs" / "worlds" / world_id / "artifacts" /
                "cultural" / city_id / "wikivoyage_evidence.json"
            )
            legacy_evidence = read_json(legacy_evidence_path) if legacy_evidence_path.is_file() else {}
            page_title = _select_region_source_page_title(
                selection, prior, legacy_evidence, cities[city_id], city_id,
            )
            world_path, city_version = self._region_active_world_path(city_id)
            base = [sys.executable, "tools/run_region_wikivoyage_source_pipeline.py", "--world-id", world_id,
                    "--city-id", city_id, "--page-title", page_title,
                    "--dump", str(PIPELINE_ROOT / "data/raw/wikivoyage/enwikivoyage/enwikivoyage-latest-pages-articles.xml.bz2"),
                    "--output-root", str(draft), "--stage", text_model, "--vision-stage", vision_model,
                    "--max-calls", "0"]
            unresolved_failures = [
                failure for failure in manifest.get("failures") or []
                if isinstance(failure, dict) and failure.get("resolved_at_epoch_s") is None
            ]
            if unresolved_failures:
                # Clicking Start again is the UI's explicit retry action.  The
                # pipeline still reuses every durable item cache and retries
                # only the previously failed logical item.
                base.append("--retry-failed")
            job = {"city_id": city_id, "city_name": cities[city_id].get("city_name") or city_id,
                   "city_version": city_version, "world_id": world_id, "page_title": page_title,
                   "text_model": text_model, "vision_model": vision_model,
                   "stage": "stage1", "status": "queued", "phase": "stage1_queued",
                   "completed": 0, "total": 0, "error": "", "output_root": str(draft), "formal_root": str(formal),
                   "media_completed": int(prior.get("media_completed") or (saved_completed if saved_phase == "media_fetch" else 0)),
                   "media_total": int(prior.get("media_total") or (saved_total if saved_phase == "media_fetch" else 0)),
                   "stage1_completed": int(prior.get("stage1_completed") or (saved_completed if saved_phase == "stage1_extraction" else 0) or cached_stage1),
                   "stage1_total": int(prior.get("stage1_total") or (saved_total if saved_phase == "stage1_extraction" else 0) or stage1_total),
                   "stage2_completed": int(prior.get("stage2_completed") or (saved_completed if saved_phase == "stage2_assessment" else 0)),
                   "stage2_total": int(prior.get("stage2_total") or (saved_total if saved_phase == "stage2_assessment" else 0)),
                   "state_path": str(state_path), "log_path": str(draft / "ui_build.log"),
                   "stage1_command": base,
                   "stage2_command": [*base, "--stage2-only", "--world", str(world_path), "--city-version", city_version],
                   "updated_at": datetime.now().isoformat(timespec="seconds")}
            write_json(state_path, {key: value for key, value in job.items() if not key.endswith("_command")})
            jobs.append(job)
        with _REGION_BUILD_LOCK:
            _REGION_BUILD_BATCHES.add(world_id)
            for job in jobs:
                _REGION_BUILD_JOBS[job["city_id"]] = job
        threading.Thread(target=_region_build_batch_worker, args=(world_id, jobs), daemon=True).start()
        self._json({"ok": True, "cities": [job["city_id"] for job in jobs], "status": "queued"})

    def _cancel_region_build(self) -> None:
        """Interrupt the current World's Region queue while preserving durable caches."""
        world_id = self._current_browsing_world_id()
        now = datetime.now().isoformat(timespec="seconds")
        with _REGION_BUILD_LOCK:
            active = world_id in _REGION_BUILD_BATCHES
            if active:
                _REGION_BUILD_CANCEL_REQUESTS.add(world_id)
            jobs = [job for job in _REGION_BUILD_JOBS.values() if job.get("world_id") == world_id]
            processes = [
                process for city_id, process in _REGION_BUILD_PROCESSES.items()
                if (_REGION_BUILD_JOBS.get(city_id) or {}).get("world_id") == world_id
            ]
            for job in jobs:
                if job.get("status") in {"queued", "running", "retry_wait"}:
                    job.update({
                        "status": "interrupted",
                        "error": "已人工中断当前 Region 城市队列，可从已有缓存继续构建。",
                        "updated_at": now,
                    })
                    write_json(Path(job["state_path"]), {key: value for key, value in job.items() if not key.endswith("_command")})
        for process in processes:
            if process.poll() is None:
                process.terminate()
        self._json({"ok": True, "status": "interrupted" if active else "not_running"})

    def _region_pipeline_root(self, city_id: str) -> Path | None:
        """Return a complete current-schema city-level Region asset root."""
        if not city_id:
            return None
        root = PIPELINE_ROOT / "outputs" / "worlds" / self._current_browsing_world_id()
        candidate = root / "artifacts" / "cultural" / city_id / "region_source_pipeline_formal"
        assessments = candidate / "regional_rule_stage2_assessments.json"
        rules = candidate / "regional_rules.json"
        if not assessments.is_file() or not rules.is_file():
            return None
        try:
            rules_asset = read_json(rules)
            assessment_asset = read_json(assessments)
        except (OSError, ValueError, FileNotFoundError):
            return None
        if rules_asset.get("schema_version") != "regional_rules.v3":
            return None
        if assessment_asset.get("schema_version") != "regional_rule_stage2_assessments.v4":
            return None
        if (assessment_asset.get("world_id") != self._current_browsing_world_id()
                or assessment_asset.get("city_id") != city_id
                or rules_asset.get("city_id") != city_id):
            return None
        rule_ids = _region_stage2_expected_rule_ids(rules_asset)
        assessment_ids = {
            str(item.get("rule_id") or "") for item in assessment_asset.get("assessments") or []
            if isinstance(item, dict) and str(item.get("rule_id") or "")
        }
        if not rule_ids or rule_ids != assessment_ids:
            return None
        return candidate

    def _serve_region_rule_cities(self) -> None:
        cities: list[dict] = []
        for city in self._current_world_cities():
            city_id = str(city.get("city_id") or "")
            root = self._region_pipeline_root(city_id)
            if root is None:
                continue
            rules = read_json(root / "regional_rules.json").get("rules") or []
            cities.append({
                "city_id": city_id,
                "city_name": city.get("city_name") or city.get("label") or city_id,
                "rule_count": len([rule for rule in rules if isinstance(rule, dict)]),
            })
        self._json({"cities": sorted(cities, key=lambda item: str(item["city_name"]).casefold())})

    @staticmethod
    def _region_human_review_path(root: Path) -> Path:
        return root / "region_human_reviews.json"

    def _load_region_human_revisions(self, root: Path) -> dict:
        path = self._region_human_review_path(root)
        if not path.is_file():
            return {}
        try:
            asset = read_json(path)
        except (OSError, ValueError, FileNotFoundError):
            return {}
        return asset.get("revisions") if isinstance(asset.get("revisions"), dict) else {}

    @staticmethod
    def _apply_region_human_revision(rule: dict, assessment: dict | None, revision: object) -> tuple[dict, dict | None]:
        """Overlay reviewer-owned fields without altering Stage-1/Stage-2 originals."""
        if not isinstance(revision, dict):
            return rule, assessment
        effective_rule = json.loads(json.dumps(rule))
        effective_assessment = json.loads(json.dumps(assessment)) if isinstance(assessment, dict) else None
        rule_override = revision.get("rule")
        if isinstance(rule_override, dict):
            for key in ("statement", "extraction_status"):
                if key in rule_override:
                    effective_rule[key] = rule_override[key]
        assessment_override = revision.get("assessment")
        if isinstance(assessment_override, dict) and effective_assessment is not None:
            for key in ("atomicity", "independent_restrictions", "checks", "status",
                        "affected_poi_type_indexes", "affected_poi_primary_types",
                        "poi_retrieval_phrases", "task_source_eligible", "reason", "human_review"):
                if key in assessment_override:
                    effective_assessment[key] = assessment_override[key]
        return effective_rule, effective_assessment

    def _serve_region_rules(self, city_id: str) -> None:
        root = self._region_pipeline_root(city_id)
        if root is None:
            self._error("该城市没有可审批的 Region 准则资产。", 404)
            return
        rules_asset = read_json(root / "regional_rules.json")
        assessment_asset = read_json(root / "regional_rule_stage2_assessments.json")
        snapshot = read_json(root / "wikivoyage_source_snapshot.json") if (root / "wikivoyage_source_snapshot.json").is_file() else {}
        evidence = {
            str(block.get("evidence_id") or ""): block
            for section in snapshot.get("sections") or [] for block in section.get("blocks") or []
            if isinstance(block, dict)
        }
        assessments = {str(item.get("rule_id") or ""): item for item in assessment_asset.get("assessments") or [] if isinstance(item, dict)}
        revisions = self._load_region_human_revisions(root)
        rows = []
        for rule in rules_asset.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            rule_id = str(rule.get("rule_id") or "")
            effective_rule, effective_assessment = self._apply_region_human_revision(rule, assessments.get(rule_id), revisions.get(rule_id))
            refs = [str(ref) for ref in effective_rule.get("evidence_refs") or []]
            rows.append({
                "rule": effective_rule,
                "assessment": effective_assessment,
                "evidence": [{"raw": evidence[ref]} for ref in refs if ref in evidence],
            })
        city = next((item for item in self._current_world_cities() if str(item.get("city_id") or "") == city_id), {})
        self._json({
            "city_id": city_id,
            "city_name": city.get("city_name") or city_id,
            "rules_schema": rules_asset.get("schema_version"),
            "assessments_schema": assessment_asset.get("schema_version"),
            "poi_type_catalog": list(assessment_asset.get("poi_type_catalog") or []),
            "rules": rows,
        })

    def _update_region_rule(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        city_id, rule_id = str(payload.get("city_id") or ""), str(payload.get("rule_id") or "")
        rule_update = payload.get("rule")
        assessment_update = payload.get("assessment")
        if not city_id or not rule_id or not isinstance(rule_update, dict) or not isinstance(assessment_update, dict):
            self._error("city_id、rule_id、地域准则和类型绑定均为必填。", 400)
            return
        root = self._region_pipeline_root(city_id)
        if root is None:
            self._error("该城市没有可审批的 Region 准则资产。", 404)
            return
        rules_path = root / "regional_rules.json"
        assessments_path = root / "regional_rule_stage2_assessments.json"
        rules_asset, assessments_asset = read_json(rules_path), read_json(assessments_path)
        rule_entry = next((item for item in rules_asset.get("rules") or [] if str(item.get("rule_id") or "") == rule_id), None)
        assessment_entry = next((item for item in assessments_asset.get("assessments") or [] if str(item.get("rule_id") or "") == rule_id), None)
        if not isinstance(rule_entry, dict) or not isinstance(assessment_entry, dict):
            self._error("未找到该地域准则或其类型绑定。", 404)
            return
        if str(rule_update.get("rule_id") or rule_id) != rule_id or str(assessment_update.get("rule_id") or rule_id) != rule_id:
            self._error("地域准则标识不可修改。", 400)
            return
        rule_update = dict(rule_update)
        assessment_update = dict(assessment_update)
        statement = str(rule_update.get("statement") or "").strip()
        extraction_status = str(rule_update.get("extraction_status") or "")
        atomicity = str(assessment_update.get("atomicity") or "")
        checks = assessment_update.get("checks")
        status = str(assessment_update.get("status") or "")
        types = sorted({str(value) for value in assessment_update.get("affected_poi_primary_types") or [] if str(value)})
        phrases = [str(value).strip() for value in assessment_update.get("poi_retrieval_phrases") or [] if str(value).strip()]
        restrictions = [str(value).strip() for value in assessment_update.get("independent_restrictions") or [] if str(value).strip()]
        catalog = list(assessments_asset.get("poi_type_catalog") or [])
        if not statement or extraction_status not in {"supported", "ambiguous", "incomplete"}:
            self._error("完整准则和有效的来源支持状态均为必填。", 400)
            return
        if atomicity not in {"atomic", "needs_split"}:
            self._error("准则原子性必须为单一准则或需要拆分。", 400)
            return
        if atomicity == "needs_split" and len(restrictions) < 2:
            self._error("需要拆分的准则必须保留至少两条拆分建议。", 400)
            return
        required_checks = ("regional_fact", "has_applicability_scope", "can_distinguish_real_choices")
        if atomicity == "atomic" and (not isinstance(checks, dict) or any(not isinstance(checks.get(key), bool) for key in required_checks)):
            self._error("单一准则必须完成人工可复核的三项 Stage 2 判断。", 400)
            return
        if any(value not in catalog for value in types):
            self._error("关联 POI 类型必须来自当前正式 Stage 2 类型目录。", 400)
            return
        allowed_statuses = {"task_source_eligible", "source_only", "needs_stage1_split", "not_bindable_in_current_map"}
        if status not in allowed_statuses:
            self._error("出题来源结论无效。", 400)
            return
        eligible = (
            extraction_status == "supported" and atomicity == "atomic" and isinstance(checks, dict)
            and all(checks.get(key) is True for key in required_checks) and bool(types)
        )
        if (status == "task_source_eligible") != eligible:
            self._error("出题来源结论必须与来源状态、原子性、三项判断及 POI 类型保持一致。", 400)
            return
        if atomicity == "needs_split" and status != "needs_stage1_split":
            self._error("需要拆分的准则必须使用“需要拆分”结论。", 400)
            return
        if status != "task_source_eligible" and types:
            self._error("不可用于出题的准则不能保留关联 POI 类型。", 400)
            return
        type_indexes = [catalog.index(value) + 1 for value in types]
        human_review = {
            **(assessment_update.get("human_review") if isinstance(assessment_update.get("human_review"), dict) else {}),
            "reviewer": "local_reviewer",
            "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        }
        rule_override = {"statement": statement, "extraction_status": extraction_status}
        assessment_override = {
            "atomicity": atomicity,
            "independent_restrictions": restrictions if atomicity == "needs_split" else [],
            "checks": ({key: checks[key] for key in required_checks} if atomicity == "atomic" else None),
            "status": status,
            "affected_poi_type_indexes": type_indexes,
            "affected_poi_primary_types": types,
            "poi_retrieval_phrases": phrases,
            "task_source_eligible": eligible,
            "reason": str(assessment_update.get("reason") or "").strip(),
        }
        assessment_override["human_review"] = human_review
        review_path = self._region_human_review_path(root)
        review_asset = read_json(review_path) if review_path.is_file() else {
            "schema_version": "geoword.region_human_reviews.v2",
            "base_rules_schema": "regional_rules.v3",
            "base_assessments_schema": "regional_rule_stage2_assessments.v4",
            "city_id": city_id,
            "revisions": {},
        }
        if review_asset.get("schema_version") != "geoword.region_human_reviews.v2":
            self._error("现有人工覆盖文件不是当前 v3 审核 schema，不能混合写入。", 409)
            return
        review_asset.setdefault("revisions", {})[rule_id] = {
            "rule": rule_override,
            "assessment": assessment_override,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        write_json(review_path, review_asset)
        effective_rule, effective_assessment = self._apply_region_human_revision(rule_entry, assessment_entry, review_asset["revisions"][rule_id])
        self._json({"ok": True, "rule": effective_rule, "assessment": effective_assessment})


    def _handle_generate(self) -> None:
        """POST /api/generate — start one observable background build job."""
        payload = self._read_json_body()
        if payload is None:
            return
        task_type = payload.get('task_type', '')
        if task_type not in {'place_grounding', 'place_discovery', 'route_planning', 'multi_entity', 'safety_planning', 'region_aware', 'itinerary'}:
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
        if task_type == "route_planning":
            preflight_id = str(payload.get("route_preflight_id") or "")
            preflight = _ROUTE_COST_PREFLIGHTS.get(preflight_id)
            expected = {
                "world_id": world_id,
                "city": city,
                "count": count,
                "mode": mode,
                "batch": requested_batch,
                "diffusion_start": route_diffusion_start,
                "pool_max": route_pool_max,
            }
            if not preflight or any(preflight.get(key) != value for key, value in expected.items()):
                self._error("当前 Routing 生成参数尚未完成有效的 Google API 成本预估。", 409)
                return
            current_cities = {item["city_id"]: item for item in self._current_world_cities()}
            if int((current_cities.get(city) or {}).get("active_version") or 0) != int(preflight["active_version"]):
                self._error("当前城市版本已变化，请重新预估 Google API 成本。", 409)
                return
            current_route_path = resolve_world_route_cache(world_id, city, int(preflight["active_version"]))
            current_route_payload = read_json(current_route_path) if current_route_path and current_route_path.is_file() else {}
            current_pool_path = current_route_path.parent / "route_poi_pool.json" if current_route_path else None
            current_pool_payload = read_json(current_pool_path) if current_pool_path and current_pool_path.is_file() else {}
            current_route_state_hash = hashlib.sha256(json.dumps(
                {"route": current_route_payload, "pool": current_pool_payload},
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            if current_route_state_hash != preflight.get("route_state_hash"):
                self._error("路线池或缓存已变化，请重新预估 Google API 成本。", 409)
                return
            settings_dir = (
                PIPELINE_ROOT / "outputs" / "worlds" / world_id / "artifacts" / "routes"
                / city / f"v{preflight['active_version']}"
            )
            route_cost_events_path = settings_dir / "route_cost_events.jsonl"
            settings_dir.mkdir(parents=True, exist_ok=True)
            write_json(settings_dir / "route_generation_settings.json", {
                "schema_version": 1,
                "diffusion_start": route_diffusion_start,
                "pool_max": route_pool_max,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
        elif task_type == "multi_entity":
            preflight_id = str(payload.get("multi_entity_preflight_id") or "")
            preflight = _MULTI_ENTITY_COST_PREFLIGHTS.get(preflight_id)
            expected = {
                "world_id": world_id, "city": city, "count": count,
                "mode": mode, "batch": requested_batch,
            }
            if not preflight or any(preflight.get(key) != value for key, value in expected.items()):
                self._error("当前 Multi-Entity 生成参数尚未完成有效的 Google API 成本预估。", 409)
                return
            current_cities = {item["city_id"]: item for item in self._current_world_cities()}
            if int((current_cities.get(city) or {}).get("active_version") or 0) != int(preflight["active_version"]):
                self._error("当前城市版本已变化，请重新预估 Google API 成本。", 409)
                return
            current_route_path = resolve_world_route_cache(world_id, city, int(preflight["active_version"]))
            route_cost_events_path = current_route_path.parent / "route_cost_events.jsonl" if current_route_path else None
            current_route_payload = _multi_entity_effective_route_payload(current_route_path)
            current_pool_path = current_route_path.parent / "route_poi_pool.json" if current_route_path else None
            current_pool_payload = read_json(current_pool_path) if current_pool_path and current_pool_path.is_file() else {}
            current_characters = self._route_city_characters(world_id, city)
            current_state_hash = hashlib.sha256(json.dumps(
                {"route": current_route_payload, "pool": current_pool_payload, "characters": current_characters},
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            if current_state_hash != preflight.get("resource_state_hash"):
                self._error("路线缓存、Routing POI池或人物资产已变化，请重新预估 Google API 成本。", 409)
                return
        elif task_type == "safety_planning":
            preflight_id = str(payload.get("safety_preflight_id") or "")
            preflight = _SAFETY_COST_PREFLIGHTS.get(preflight_id)
            expected = {
                "world_id": world_id, "city": city, "count": count,
                "mode": mode, "batch": requested_batch,
            }
            if not preflight or any(preflight.get(key) != value for key, value in expected.items()):
                self._error("当前 Safety Planning 生成参数尚未完成有效的 Google API 成本预估。", 409)
                return
            current_cities = {item["city_id"]: item for item in self._current_world_cities()}
            if int((current_cities.get(city) or {}).get("active_version") or 0) != int(preflight["active_version"]):
                self._error("当前城市版本已变化，请重新预估 Google API 成本。", 409)
                return
            current_route_path = self._safety_bound_route(world_id, city)
            if current_route_path is None:
                return
            route_cost_events_path = current_route_path.parent / "route_cost_events.jsonl" if current_route_path else None
            current_route_payload = _multi_entity_effective_route_payload(current_route_path)
            current_pool_path = current_route_path.parent / "route_poi_pool.json" if current_route_path else None
            current_pool_payload = read_json(current_pool_path) if current_pool_path and current_pool_path.is_file() else {}
            current_world = self._route_city_world(world_id, city)
            current_characters = self._route_city_characters(world_id, city)
            current_state_hash = hashlib.sha256(json.dumps(
                {"route": current_route_payload, "pool": current_pool_payload,
                 "world": current_world, "characters": current_characters},
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            if current_state_hash != preflight.get("resource_state_hash"):
                self._error("路线缓存、Routing POI池、地点或人物资产已变化，请重新预估 Google API 成本。", 409)
                return
        elif task_type == "itinerary":
            preflight_id = str(payload.get("itinerary_preflight_id") or "")
            preflight = _ITINERARY_COST_PREFLIGHTS.get(preflight_id)
            expected = {
                "world_id": world_id, "city": city, "count": count,
                "mode": mode, "batch": requested_batch,
            }
            if not preflight or any(preflight.get(key) != value for key, value in expected.items()):
                self._error("当前 Itinerary 生成参数尚未完成有效的 Google API 成本预估。", 409)
                return
            current_cities = {item["city_id"]: item for item in self._current_world_cities()}
            if int((current_cities.get(city) or {}).get("active_version") or 0) != int(preflight["active_version"]):
                self._error("当前城市版本已变化，请重新预估 Google API 成本。", 409)
                return
            current_route_path = resolve_world_route_cache(world_id, city, int(preflight["active_version"]))
            route_cost_events_path = current_route_path.parent / "route_cost_events.jsonl" if current_route_path else None
            current_route_payload = _multi_entity_effective_route_payload(current_route_path)
            current_pool_path = current_route_path.parent / "route_poi_pool.json" if current_route_path else None
            current_pool_payload = read_json(current_pool_path) if current_pool_path and current_pool_path.is_file() else {}
            current_world = self._route_city_world(world_id, city)
            current_characters = self._route_city_characters(world_id, city)
            current_state_hash = hashlib.sha256(json.dumps(
                {"route": current_route_payload, "pool": current_pool_payload,
                 "world": current_world, "characters": current_characters},
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            if current_state_hash != preflight.get("resource_state_hash"):
                self._error("路线缓存、Routing POI池、地点或人物资产已变化，请重新预估 Google API 成本。", 409)
                return
        elif task_type == "region_aware":
            preflight_id = str(payload.get("region_preflight_id") or "")
            preflight = _REGION_COST_PREFLIGHTS.get(preflight_id)
            expected = {
                "world_id": world_id, "city": city, "count": count,
                "mode": mode, "batch": requested_batch,
            }
            if not preflight or any(preflight.get(key) != value for key, value in expected.items()):
                self._error("当前 Region 生成参数尚未完成有效的 Google API 成本预估。", 409)
                return
            current_cities = {item["city_id"]: item for item in self._current_world_cities()}
            version = int((current_cities.get(city) or {}).get("active_version") or 0)
            if version != int(preflight["active_version"]):
                self._error("当前城市版本已变化，请重新预估 Google API 成本。", 409)
                return
            current_route_path = resolve_world_route_cache(world_id, city, version)
            route_cost_events_path = current_route_path.parent / "route_cost_events.jsonl" if current_route_path else None
            current_route_payload = _multi_entity_effective_route_payload(current_route_path)
            current_pool_path = current_route_path.parent / "route_poi_pool.json" if current_route_path else None
            current_pool_payload = read_json(current_pool_path) if current_pool_path and current_pool_path.is_file() else {}
            current_world = self._route_city_world(world_id, city)
            current_characters = self._route_city_characters(world_id, city)
            artifacts = self._active_city_artifacts(world_id, city, version)
            service_path = self._registered_artifact_path(artifacts, "service_detail_cache")
            pipeline_root = self._region_pipeline_root(city)
            if service_path is None or pipeline_root is None:
                self._error("Region 构建资源已变化或不完整，请重新检查城市资源。", 409)
                return
            current_state_hash = hashlib.sha256(json.dumps(
                {"route": current_route_payload, "pool": current_pool_payload,
                 "world": current_world, "characters": current_characters,
                 "rules": read_json(pipeline_root / "regional_rules.json"),
                 "stage2": read_json(pipeline_root / "regional_rule_stage2_assessments.json"),
                 "service_detail": read_json(service_path)},
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            if current_state_hash != preflight.get("resource_state_hash"):
                self._error("路线、地点、人物、地域规则或服务详情资产已变化，请重新预估 Google API 成本。", 409)
                return
        batch_dir = managed_batch_dir(self.benchmarks_root, task_type, world_id, city, batch)
        tasks_dir = batch_dir / 'tasks'
        baseline_folders = [entry.name for entry in tasks_dir.iterdir()] if tasks_dir.exists() else []
        generator_target_count = _generation_target_count(batch_dir, task_type, mode, count)
        job_id = f"gen_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        jobs_root = _generation_jobs_root()
        job = {'job_id': job_id, 'status': 'queued', 'phase': 'queued', 'phase_label': '等待启动构建', 'detail': '正在准备后台构建进程', 'task_type': task_type, 'city': city, 'world_id': world_id, 'batch': batch, 'mode': mode, 'model_label': model_label, 'requested_count': count, 'generator_target_count': generator_target_count, 'infeasible_percent': infeasible_percent, 'construction_count': 0, 'candidate_count': 0, 'completed_count': 0, 'observed_count': 0, 'rejected_count': 0, 'generated_folders': [], 'baseline_folders': baseline_folders, '_observed_folders': [], 'started_at': datetime.now().isoformat(timespec='seconds'), 'last_activity_at': datetime.now().isoformat(timespec='seconds'), '_started_monotonic': time.monotonic(), 'output_dir': str(batch_dir), '_state_path': str(jobs_root / f'{job_id}.json'), '_log_path': str(jobs_root / f'{job_id}.log'), 'route_cost_events_path': str(route_cost_events_path) if route_cost_events_path else ''}
        cmd = [sys.executable, '-m', 'task_module.generator', '--city', city, '--world-id', world_id, '--types', task_type, '--counts', str(generator_target_count), '--output', 'outputs/benchmarks', '--stage-model', stage_model, '--batch', batch, '--infeasible-percent', str(infeasible_percent)]
        if task_type == 'route_planning':
            cmd.extend(['--route-diffusion-start', str(route_diffusion_start), '--route-pool-max', str(route_pool_max),
                        '--route-preflight-seed', str(preflight['batch_seed'])])
        elif task_type == 'multi_entity':
            cmd.extend(['--multi-entity-preflight-seed', str(preflight['batch_seed'])])
        elif task_type == 'safety_planning':
            cmd.extend(['--safety-preflight-seed', str(preflight['batch_seed'])])
        elif task_type == 'itinerary':
            cmd.extend(['--itinerary-preflight-seed', str(preflight['batch_seed'])])
        elif task_type == 'region_aware':
            cmd.extend(['--region-preflight-seed', str(preflight['batch_seed'])])
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
