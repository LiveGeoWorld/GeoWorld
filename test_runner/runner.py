"""Runner — CLI entry for benchmark test execution.

Usage:
    python -m test_runner.runner --task tasks/task_0001/ --routes outputs/routes/{city}/route_cache.json
    python -m test_runner.runner --tasks tasks/route_planning/ --routes outputs/routes/{city}/route_cache.json
    python -m test_runner.runner --tasks tasks/route_planning/ --routes ... --output my_results.json

Architecture:
    Agent → MapApp (route_cache.json) → tools: search_places/query_routes/get_place_detail
    Agent → UserInterfaceApp → final_answer(plan)
    Evaluator → route_sandbox.json → GT verification

Auto-saves results to:
    outputs/evaluations/{task_type}/{world_id}/{city_id}/{batch}/{run_id}/
        results.json
        tasks/0001.json
        tasks/task_index.json
"""
from __future__ import annotations
import logging
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import hashlib
from typing import Any, Callable
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from shared.config_loader import PipelineConfig
from shared.io import read_json, write_json
from test_runner.verification.shared.formalization import is_formal_task
from app.runtime.asset_bindings import _resolve_route_cache, _resolve_place_world_snapshot, _sha256_file, _task_batch_provenance
from app.runtime.environment import Environment
from app.runtime.contracts import load_agent_environment
from app.apps.map_app import MapAppSandbox
from app.apps.user_interface import UserInterfaceApp
from app.apps.local_service import LocalServiceApp
from app.apps.messaging import MessagingApp
from app.apps.calendar import CalendarApp
from app.apps.weather_app import WeatherApp
from app.apps.city_news import CityNewsApp
from test_runner.agent.llm_agent import LLMAgent
from test_runner.eval.evaluator import evaluate as evaluate_route
from test_runner.eval.multi_entity_metrics import check_feasible_match, check_restaurant_match, check_budget_valid, check_dietary_satisfied, check_time_window_valid, check_commute_feasible, check_notification_complete, check_assignment_complete
from test_runner.place.runner import _write_jsonl
from test_runner.eval.itinerary_metrics import check_feasible_consistency, check_itinerary_completeness, check_time_window_valid as check_itinerary_time_window, check_event_order_valid, check_spatial_coherence, check_budget_valid as check_itinerary_budget, check_constraint_satisfaction, check_tool_efficiency
from test_runner.eval.safety_metrics import check_feasible_consistency as check_safety_feasible, check_risk_identified, check_safe_alternative, check_risk_integrated, check_original_rejected, check_risk_rationale, check_interference_avoided, check_tool_efficiency as check_safety_efficiency
from test_runner.eval.region_metrics import check_feasible_consistency as check_region_feasible, check_cultural_time_valid, check_cultural_venue_valid, check_cultural_decision_integrated, check_constraint_ids_applied, check_wrong_plans_avoided, check_region_decision_success, check_tool_efficiency as check_region_efficiency
from test_runner.eval.replan_metrics import check_transition_proof_valid, check_base_constraints_still_met, check_disruption_detected, check_replan_executed, check_alternative_valid, check_event_progression, check_notification_complete as check_replan_notify, check_tool_efficiency as check_replan_efficiency
from test_runner.eval.structured_output import check_required_structured_output
from test_runner.eval.place_discovery_metrics import check_discovery_answer
from test_runner.place.judge_sequence import evaluate_judge_sequence
from test_runner.place.runtime import AppRuntime
from test_runner.place.sandbox_adapter import PlaceGroundingSandbox, PlaceRuntimeSandbox, discovery_route_evidence
from test_runner.place.session import load_session
from test_runner.schema import AgentRunResult, MetricResult, RunSummary

def run_task(task_dir: str | Path, route_cache_path: str='', model_key: str='cliproxy_gpt55_pro', config_yaml: str='config.yaml', world_path: str='', dynamic_event_mode: str='shared_world', recovery_record: dict[str, Any] | None=None, checkpoint_sink=None, dynamic_information_mode: str='pull') -> AgentRunResult:
    """Run a single task against its provenance-bound frozen map world."""
    task_dir = Path(task_dir)
    from app.runtime.shared_events import validate_event_mode
    validate_event_mode(dynamic_event_mode)
    from app.runtime.explicit_updates import validate_information_mode
    validate_information_mode(dynamic_information_mode)
    if recovery_record is not None and recovery_record.get('dynamic_information_mode', 'pull') != dynamic_information_mode:
        raise ValueError('Recovery dynamic information channel mismatch')
    task_data = read_json(task_dir / 'task.json')
    user_prompt = task_data.get('user_prompt', '')
    task_id = task_data.get('task_id', task_dir.name)
    task_type = task_data.get('task_type', '')
    if dynamic_information_mode != 'pull' and task_type != 'dynamic_replanning':
        raise ValueError('Explicit-Update applies only to dynamic_replanning tasks')
    environment_contract = load_agent_environment(task_dir, task_type)
    if task_type in {'place_grounding', 'place_discovery'}:
        place_world_binding = _resolve_place_world_snapshot(world_path, task_dir, task_data)
        session = load_session(task_dir, task_dir.name, place_world_binding)
        runtime = AppRuntime(session)
        place_env = Environment()
        if task_type == 'place_grounding':
            runtime_sandbox = PlaceGroundingSandbox(runtime)
            place_env.terminate_on_invalid_tool = True
            place_env.register_app(runtime_sandbox)
            place_env.register_app(PlaceRuntimeSandbox(runtime, include_map=False))
            place_env.register_app(MessagingApp(str(task_dir)))
            place_env.register_app(CalendarApp(str(task_dir)))
            place_env.register_app(WeatherApp(str(task_dir)))
            place_env.register_app(CityNewsApp(str(task_dir)))
            place_env.configure_application_catalog(environment_contract['applications'], always_available_tools={('UserInterfaceApp', 'final_answer')})
            agent = LLMAgent(place_env, user_prompt, model_key=model_key, config_yaml=config_yaml, max_steps=10, input_images=_task_input_images(task_dir, task_data), task_type=task_type)
        else:
            route_map = MapAppSandbox(world_paths=place_world_binding['world_paths'], route_cache_paths=place_world_binding.get('route_cache_paths', []), route_cache_path=route_cache_path if place_world_binding['world_id'] == 'legacy' else '', route_cache_data=discovery_route_evidence(session), prefer_inline_routes=True)
            runtime_sandbox = PlaceRuntimeSandbox(runtime, route_map=route_map)
            place_env.register_app(runtime_sandbox)
            place_env.register_app(UserInterfaceApp())
            place_env.register_app(MessagingApp(str(task_dir)))
            place_env.register_app(CalendarApp(str(task_dir)))
            place_env.register_app(WeatherApp(str(task_dir)))
            place_env.register_app(CityNewsApp(str(task_dir)))
            place_env.configure_application_catalog(environment_contract['applications'], always_available_tools={('UserInterfaceApp', 'final_answer')})
            agent = LLMAgent(place_env, user_prompt, model_key=model_key, config_yaml=config_yaml, input_images=_task_input_images(task_dir, task_data), task_type=task_type)
        if recovery_record is not None:
            from test_runner.recover_task import verify_binding
            verify_binding(recovery_record, task_data, place_world_binding, dynamic_event_mode, read_json(task_dir / 'task_provenance.json') if (task_dir / 'task_provenance.json').exists() else {})
        result = agent.run(recovery=recovery_record, checkpoint_sink=checkpoint_sink) if recovery_record is not None or checkpoint_sink is not None else agent.run()
        if task_type == "place_discovery":
            metric = check_discovery_answer(result, session.gold)
        else:
            result.final_answer = runtime_sandbox.final_answer_data
            judge = evaluate_judge_sequence(session.judge_sequence, runtime_sandbox.events)
            metric = MetricResult(
                metric="judge_sequence", label="轨迹完整性（辅助指标）",
                pass_=bool(judge["success"]), detail=judge["rationale"],
            )
        result.metrics = [metric]
        result.total_metrics = 1
        result.passed_metrics = int(metric.pass_)
        result.task_id = task_id
        result.task_folder = task_dir.name
        result.task_type = task_type
        result.model_name = result.model_name or model_key
        result.evaluation_world_snapshot = {'world_id': place_world_binding.get('world_id', ''), 'city_versions': place_world_binding.get('city_versions', {}), 'world_hashes': place_world_binding.get('world_hashes', {}), 'city_count': len(place_world_binding.get('city_versions', {}))}
        return result
    if task_type == 'route_planning':
        gt_file = 'route_sandbox.json'
    else:
        gt_file = 'task_sandbox.json'
    gt_path = task_dir / gt_file
    gt_data = read_json(gt_path) if gt_path.exists() else {}
    from app.runtime.episode import load_episode
    episode = load_episode(task_dir, route_cache_path=route_cache_path, world_path=world_path, dynamic_event_mode=dynamic_event_mode, dynamic_information_mode=dynamic_information_mode, task_data=task_data, gt_data=gt_data)
    env = episode['env']
    map_world_binding = episode['binding']
    if task_type == 'dynamic_replanning':
        event_snapshot = episode['events']
        proof_request_path = task_dir / 'counterfactual_proof_request.json'
        proof = gt_data.get('counterfactual_proof', {})
        if proof_request_path.exists() and isinstance(proof, dict):
            from test_runner.verification.dynamic_replanning.counterfactual_proof import validate_proof
            gt_data['_dynamic_proof_validation'] = validate_proof(read_json(proof_request_path), proof)
        else:
            gt_data['_dynamic_proof_validation'] = {'valid': False, 'errors': ['missing_dynamic_counterfactual_proof']}
        if recovery_record is not None:
            saved_events = (recovery_record.get('evaluation_world_snapshot') or {}).get('dynamic_events') or {}
            if saved_events.get('ledger_sha256') and saved_events['ledger_sha256'] != event_snapshot.get('ledger_sha256'):
                raise ValueError('Recovery frozen event ledger hash mismatch')
    agent = LLMAgent(env, user_prompt, model_key=model_key, config_yaml=config_yaml, max_steps=250 if task_type == 'itinerary' else 50, input_images=_task_input_images(task_dir, task_data), task_type=task_type)
    if recovery_record is not None:
        from test_runner.recover_task import verify_binding
        verify_binding(recovery_record, task_data, map_world_binding, dynamic_event_mode, read_json(task_dir / 'task_provenance.json') if (task_dir / 'task_provenance.json').exists() else {})
    result = agent.run(recovery=recovery_record, checkpoint_sink=checkpoint_sink) if recovery_record is not None or checkpoint_sink is not None else agent.run()
    result.task_id = task_id
    result.task_folder = task_dir.name
    result.task_type = task_type
    if task_type == 'dynamic_replanning':
        result.evaluation_world_snapshot['dynamic_events'] = event_snapshot
        event_snapshot['dynamic_information_mode'] = dynamic_information_mode
        result.dynamic_event_mode = dynamic_event_mode
    result.gt_route_summary = gt_data.get('correct_route_summary', {})
    result.core_places = gt_data.get('core_places', [])
    if gt_data.get('correct_route_id'):
        result.gt_route_id = gt_data['correct_route_id']
    result.gt_leg_route_ids = gt_data.get('correct_leg_route_ids', [])
    task_type = task_data.get('task_type', '')
    if task_type == 'multi_entity':
        _evaluate_multi_entity(result, gt_data)
    elif task_type == 'itinerary':
        _evaluate_itinerary(result, gt_data, task_dir=task_dir)
    elif task_type == 'safety_planning':
        _evaluate_safety(result, gt_data)
    elif task_type == 'region_aware':
        _evaluate_region(result, gt_data)
    elif task_type == 'dynamic_replanning':
        _evaluate_dynamic(result, gt_data)
    else:
        evaluate_route(result, str(gt_path))
    _append_output_contract(result, task_type, gt_data)
    return result

def _resolve_place_world(path: str | None, task_dir: str | Path, task_data: dict[str, Any]) -> str:
    """Compatibility wrapper returning the task city artifact path.

    New execution must use ``_resolve_place_world_snapshot`` so Agents see the
    complete frozen world.  Older callers/tests that need a single path retain
    their former contract.
    """
    snapshot = _resolve_place_world_snapshot(path, task_dir, task_data)
    task_city = str(task_data.get('city_id') or '')
    if task_city:
        for world_path in snapshot['world_paths']:
            try:
                cities = read_json(world_path).get('cities') or []
            except Exception:
                continue
            if any((city.get('city_id') == task_city for city in cities if isinstance(city, dict))):
                return world_path
    return snapshot['world_paths'][0]

def _task_input_images(task_dir: Path, task_data: dict[str, Any]) -> list[Path]:
    """Resolve declared Agent-visible task images without admitting sidecar hints.

    ``summary.input_assets.image_path`` is the sole evaluation-image contract.
    In particular, ``prompt_reference_image_path`` is an authoring asset and
    must never enter an Agent request.
    """
    summary = task_data.get('summary') if isinstance(task_data.get('summary'), dict) else {}
    assets = summary.get('input_assets') if isinstance(summary.get('input_assets'), dict) else {}
    image_path = assets.get('image_path')
    if not image_path:
        return []
    declared = Path(str(image_path))
    if declared.is_absolute():
        raise ValueError('Task input image path must be relative to its task directory.')
    root = task_dir.resolve()
    resolved = (root / declared).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError('Task input image path escapes its task directory.') from exc
    if not resolved.is_file():
        raise FileNotFoundError(f'Declared task input image is missing: {declared}')
    return [resolved]

def _filter_approved_folders(folders: list[Path], review_path: Path) -> list[Path]:
    """Return only explicitly approved task folders; absent review data approves none."""
    review_data = read_json(review_path) if review_path.is_file() else {}
    return [folder for folder in folders if (review_data.get(folder.name) or {}).get('status') == 'approved']

def run_all_tasks(tasks_dir: str | Path | list[str | Path], route_cache_path: str='', model_key: str='cliproxy_gpt55_pro', config_yaml: str='config.yaml', output_path: str | None=None, world_path: str='', approved_only: bool=False, task_type_filter: str='', selected_task_folders: list[str | Path] | None=None, progress_callback: Callable[[dict[str, Any]], None] | None=None, dynamic_event_mode: str='shared_world', dynamic_information_mode: str='pull') -> tuple[list[AgentRunResult], RunSummary]:
    """Run a formal batch or composed benchmark without copying task bundles."""
    raw_task_dirs = [tasks_dir] if isinstance(tasks_dir, (str, Path)) else list(tasks_dir)
    from app.runtime.shared_events import validate_event_mode
    validate_event_mode(dynamic_event_mode)
    from app.runtime.explicit_updates import validate_information_mode
    validate_information_mode(dynamic_information_mode)
    task_dirs = []
    for raw in raw_task_dirs:
        candidate = Path(raw)
        if candidate.is_dir() and candidate not in task_dirs:
            task_dirs.append(candidate)
    if not task_dirs:
        raise ValueError('No task directories were supplied for this run.')
    results: list[AgentRunResult] = []
    batch_provenances = [_task_batch_provenance(path) for path in task_dirs]
    world_ids = {item['world_id'] for item in batch_provenances if item['world_id']}
    if len(world_ids) > 1:
        raise ValueError(f'A composed benchmark cannot mix worlds: {sorted(world_ids)}')
    required_city_versions: dict[str, int] = {}
    for provenance in batch_provenances:
        for (city_id, version) in provenance['required_city_versions'].items():
            required_city_versions[city_id] = max(required_city_versions.get(city_id, 0), version)
    cache_path = _resolve_route_cache(route_cache_path, task_dirs[0]) if route_cache_path else ''
    parts = list(task_dirs[0].parts)
    benchmark_root = task_dirs[0]
    city_ids: set[str] = set()
    for (i, p) in enumerate(parts):
        if p == 'benchmarks' and i + 5 < len(parts) and (parts[i + 5] == 'tasks'):
            city_ids.add(parts[i + 3])
            benchmark_root = Path(*parts[:i + 5])
            break
    for source_dir in task_dirs[1:]:
        source_parts = list(source_dir.parts)
        for (i, part) in enumerate(source_parts):
            if part == 'benchmarks' and i + 5 < len(source_parts) and (source_parts[i + 5] == 'tasks'):
                city_ids.add(source_parts[i + 3])
                break
    run_dir = None
    if output_path:
        out = Path(output_path)
        run_dir = out.parent
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        run_dir = benchmark_root / 'runs' / f'{ts}_{model_key}_{dynamic_event_mode}'
        if dynamic_information_mode != 'pull':
            run_dir = run_dir.with_name(run_dir.name + '_' + dynamic_information_mode)
    if run_dir:
        run_dir.mkdir(parents=True, exist_ok=True)
        settings_path = run_dir / 'evaluation_settings.json'
        if settings_path.exists():
            previous_settings = read_json(settings_path)
            previous_mode = previous_settings.get('dynamic_event_mode', 'shared_world')
            if previous_mode != dynamic_event_mode:
                raise ValueError('Evaluation directory belongs to another dynamic event mode')
            if previous_settings.get('dynamic_information_mode', 'pull') != dynamic_information_mode:
                raise ValueError('Evaluation directory belongs to another dynamic information channel')
        write_json(settings_path, {'dynamic_event_mode': dynamic_event_mode, 'dynamic_information_mode': dynamic_information_mode})
        task_results_dir = run_dir / 'tasks'
        task_results_dir.mkdir(parents=True, exist_ok=True)
        task_result_index: list[dict[str, str]] = []
        logger = logging.getLogger('test_runner')
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            if isinstance(handler, logging.FileHandler):
                handler.close()
        log_path = run_dir / 'run.log'
        fh = logging.FileHandler(str(log_path), encoding='utf-8')
        fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(fh)
        logger.addHandler(logging.StreamHandler(sys.stdout))
        logger.setLevel(logging.INFO)
        logger.propagate = False
    logger.info('[Runner] source batches: %s', ', '.join((str(path.parent) for path in task_dirs)))
    logger.info(f"[Runner] route_cache override: {cache_path or '(per-source resolution)'}")
    model_name = model_key
    try:
        cfg = PipelineConfig(config_yaml)
        ep = cfg.model_endpoint(model_key)
        model_name = ep.get('model', model_key)
    except Exception:
        pass
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    folders: list[tuple[Path, Path]] = []
    if selected_task_folders is None:
        for source_dir in task_dirs:
            folders.extend(((folder, source_dir) for folder in sorted(source_dir.iterdir()) if folder.is_dir() and is_formal_task(folder)))
    else:
        source_by_path = {path.resolve(): path for path in task_dirs}
        seen: set[Path] = set()
        for raw_folder in selected_task_folders:
            folder = Path(raw_folder).resolve()
            source_dir = source_by_path.get(folder.parent.resolve())
            if source_dir is None:
                raise ValueError(f'Selected task is outside declared source batches: {folder}')
            if folder in seen:
                raise ValueError(f'Selected task is duplicated: {folder}')
            if not is_formal_task(folder):
                raise ValueError(f'Selected task is no longer formal: {folder}')
            seen.add(folder)
            folders.append((folder, source_dir))
        folders.sort(key=lambda pair: _stable_task_sort_key(pair[0], pair[1]))
    valid_folders = []
    invalidated_folders = []
    for (folder, source_dir) in folders:
        sandbox_path = folder / 'task_sandbox.json'
        if sandbox_path.exists():
            try:
                sandbox = read_json(sandbox_path)
                state = sandbox.get('audit_state', {})
                if sandbox.get('task_type') == 'region_aware' and state.get('status') == 'needs_rebuild':
                    invalidated_folders.append(f'{source_dir.parent.name}/{folder.name}')
                    continue
            except Exception:
                pass
        valid_folders.append((folder, source_dir))
    folders = valid_folders
    if invalidated_folders:
        logger.warning('[Runner] skipped %d invalidated region tasks: %s', len(invalidated_folders), ', '.join(invalidated_folders))
    if approved_only:
        approved = []
        for (folder, source_dir) in folders:
            review_path = source_dir.parent / 'review_status.json'
            if folder in _filter_approved_folders([folder], review_path):
                approved.append((folder, source_dir))
        folders = approved
    if task_type_filter:
        filtered = []
        for (f, source_dir) in folders:
            tj_path = f / 'task.json'
            if tj_path.exists():
                try:
                    tj = read_json(tj_path)
                    if tj.get('task_type') == task_type_filter:
                        filtered.append((f, source_dir))
                except Exception:
                    pass
        folders = filtered
    total = len(folders)
    if progress_callback:
        progress_callback({'phase': 'running', 'completed_count': 0, 'total_count': total, 'current_task_id': '', 'passed': 0, 'failed': 0, 'infrastructure_failed': 0, 'token_usage': {'prompt': 0, 'completion': 0, 'total': 0}})
    for (i, (folder, source_dir)) in enumerate(folders):
        tf = folder / 'task.json'
        if not tf.exists():
            continue
        task_type = ''
        try:
            task_type = str(read_json(tf).get('task_type') or '')
        except Exception:
            pass
        source_cache = '' if task_type in {'place_grounding', 'place_discovery'} else cache_path or _resolve_route_cache('', source_dir)
        result = run_task(folder, route_cache_path=source_cache, model_key=model_key, config_yaml=config_yaml, world_path=world_path, dynamic_event_mode=dynamic_event_mode, dynamic_information_mode=dynamic_information_mode)
        result.source_batch_id = source_dir.parent.name
        result.dynamic_event_mode = dynamic_event_mode
        result.source_tasks_dir = str(source_dir)
        results.append(result)
        if run_dir:
            tdir = task_results_dir
            tdir.mkdir(parents=True, exist_ok=True)
            result_payload = _result_to_dict(result)
            artifact_name = f'{len(results):04d}.json'
            write_json(tdir / artifact_name, result_payload)
            task_result_index.append({'task_id': str(result.task_id), 'task_type': str(result.task_type), 'file_name': artifact_name})
            write_json(tdir / 'task_index.json', {'schema_version': 'geoword.evaluation_task_index.v1', 'entries': task_result_index})
            with (run_dir / 'results.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(result_payload, ensure_ascii=False) + '\n')
        if progress_callback:
            partial_tokens = {'prompt': 0, 'completion': 0, 'total': 0}
            for partial in results:
                for key in partial_tokens:
                    partial_tokens[key] += partial.tokens_used.get(key, 0)
            progress_callback({'phase': 'running', 'completed_count': len(results), 'total_count': total, 'current_task_id': result.task_id, 'passed': sum((1 for item in results if item.grade == 'PASS')), 'failed': sum((1 for item in results if item.grade not in {'PASS', 'INFRASTRUCTURE_FAILURE'})), 'infrastructure_failed': sum((1 for item in results if item.grade == 'INFRASTRUCTURE_FAILURE')), 'token_usage': partial_tokens, 'last_result': _result_to_dict(result)})
        if (i + 1) % max(1, total // 10) == 0 or total <= 10:
            msg = f"  [{i + 1}/{total}] {result.task_id}: {result.passed_metrics}/{result.total_metrics} ({result.total_score}%) [{result.duration_s}s, {result.tokens_used.get('total', 0)} tokens]"
            print(msg)
    total_dur = round(time.time() - t0, 2)
    finished_at = datetime.now(timezone.utc).isoformat()
    passed = sum((1 for r in results if r.grade == 'PASS'))
    infrastructure_failed = sum((1 for r in results if r.grade == 'INFRASTRUCTURE_FAILURE'))
    grades: dict[str, int] = {}
    for r in results:
        grades[r.grade] = grades.get(r.grade, 0) + 1
    total_tokens = {'prompt': 0, 'completion': 0, 'total': 0}
    for r in results:
        t = r.tokens_used
        total_tokens['prompt'] += t.get('prompt', 0)
        total_tokens['completion'] += t.get('completion', 0)
        total_tokens['total'] += t.get('total', 0)
    print(f'\nSummary: {passed}/{len(results)} PASS ({100 * passed // max(1, len(results))}%)')
    if infrastructure_failed:
        print(f'Infrastructure failures: {infrastructure_failed}')
    print(f"Tokens: {total_tokens['total']} total ({total_tokens['prompt']} prompt + {total_tokens['completion']} completion)")
    print(f'Duration: {total_dur}s')
    logger.info(f"Summary: {passed}/{len(results)} PASS | {total_tokens['total']} tokens | {total_dur}s")
    tasks_by_type: dict[str, dict] = {}
    for r in results:
        rdict = _result_to_dict(r)
        tt = rdict.get('task_type', 'unknown')
        if tt not in tasks_by_type:
            tasks_by_type[tt] = {'task_count': 0, 'passed': 0, 'token_usage': {'prompt': 0, 'completion': 0, 'total': 0}}
        tasks_by_type[tt]['task_count'] += 1
        if rdict.get('grade') == 'PASS':
            tasks_by_type[tt]['passed'] += 1
        t = r.tokens_used
        tasks_by_type[tt]['token_usage']['prompt'] += t.get('prompt', 0)
        tasks_by_type[tt]['token_usage']['completion'] += t.get('completion', 0)
        tasks_by_type[tt]['token_usage']['total'] += t.get('total', 0)
    summary = RunSummary(dynamic_event_mode=dynamic_event_mode, dynamic_information_mode=dynamic_information_mode, run_id=run_dir.name if run_dir else '', model_key=model_key, model_name=model_name, city_id=','.join(sorted(city_ids)) or 'unknown', world_id=next(iter(world_ids), ''), required_city_versions=required_city_versions, task_batch_provenance_path=';'.join((item['path'] for item in batch_provenances if item['path'])), started_at=started_at, finished_at=finished_at, total_duration_s=total_dur, token_usage=total_tokens, task_count=len(results), passed=passed, infrastructure_failed=infrastructure_failed, grades=grades, tasks=[_result_to_dict(r) for r in results], tasks_by_type=tasks_by_type)
    from dataclasses import asdict
    if run_dir:
        write_json(run_dir / 'results.json', asdict(summary))
    if progress_callback:
        progress_callback({'phase': 'completed', 'completed_count': len(results), 'total_count': len(results), 'current_task_id': '', 'passed': passed, 'failed': len(results) - passed - infrastructure_failed, 'infrastructure_failed': infrastructure_failed, 'token_usage': total_tokens, 'summary': asdict(summary)})
    if run_dir:
        logger.removeHandler(fh)
        fh.close()
    return (results, summary)

def _stable_task_sort_key(folder: Path, source_dir: Path) -> tuple[str, str, str]:
    """Stable ordering for an explicitly recorded evaluation sample."""
    try:
        task_id = str(read_json(folder / 'task.json').get('task_id') or folder.name)
    except Exception:
        task_id = folder.name
    return (str(source_dir.resolve()), task_id, folder.name)

def _evaluate_multi_entity(result: AgentRunResult, gt_data: dict) -> None:
    """Use public POI correctness as the primary Multi-Entity decision."""
    place_match = check_restaurant_match(result, gt_data)
    metrics = [check_feasible_match(result, gt_data), place_match, check_budget_valid(result, gt_data), check_dietary_satisfied(result, gt_data), check_time_window_valid(result, gt_data), check_commute_feasible(result, gt_data), check_assignment_complete(result, gt_data), check_notification_complete(result, gt_data)]
    result.metrics = metrics
    result.total_metrics = 1
    result.passed_metrics = int(place_match.pass_)

def _evaluate_itinerary(result: AgentRunResult, gt_data: dict, *, task_dir=None) -> None:
    """Judge the published ordered POI choices against frozen witnesses."""
    from test_runner.eval.itinerary_choice import check_itinerary_choice
    result.metrics = [check_itinerary_choice(result, gt_data, task_dir=task_dir)]
    result.total_metrics = 1
    result.passed_metrics = int(result.metrics[0].pass_)

def _evaluate_safety(result: AgentRunResult, gt_data: dict) -> None:
    """Run safety-aware planning evaluation."""
    if isinstance(gt_data.get('safety_choice'), dict):
        from test_runner.eval.safety_choice import check_safety_choice
        result.metrics = [check_safety_choice(result, gt_data)]
        result.total_metrics = 1
        result.passed_metrics = int(result.metrics[0].pass_)
        return
    metrics = [check_safety_feasible(result, gt_data), check_risk_identified(result, gt_data), check_risk_integrated(result, gt_data), check_safe_alternative(result, gt_data), check_original_rejected(result, gt_data), check_risk_rationale(result, gt_data), check_interference_avoided(result, gt_data), check_safety_efficiency(result, gt_data)]
    result.metrics = metrics
    result.total_metrics = len(metrics)
    result.passed_metrics = len([m for m in metrics if m.pass_])

def _evaluate_region(result: AgentRunResult, gt_data: dict) -> None:
    """Run Region evaluation, preserving legacy planning-task readability."""
    if isinstance(gt_data.get('region_decision'), dict):
        result.metrics = [check_region_decision_success(result, gt_data)]
        result.total_metrics = 1
        result.passed_metrics = 1 if result.metrics[0].pass_ else 0
        return
    metrics = [check_region_feasible(result, gt_data), check_cultural_time_valid(result, gt_data), check_cultural_venue_valid(result, gt_data), check_cultural_decision_integrated(result, gt_data), check_wrong_plans_avoided(result, gt_data), check_region_efficiency(result, gt_data)]
    result.metrics = metrics
    result.total_metrics = len(metrics)
    result.passed_metrics = len([m for m in metrics if m.pass_])

def _evaluate_dynamic(result: AgentRunResult, gt_data: dict) -> None:
    """Run dynamic replanning evaluation."""
    metrics = [check_transition_proof_valid(result, gt_data), *([] if result.answer_versions else [check_base_constraints_still_met(result, gt_data)]), check_disruption_detected(result, gt_data), check_replan_executed(result, gt_data), *([] if result.answer_versions else [check_event_progression(result, gt_data), check_alternative_valid(result, gt_data)]), check_replan_notify(result, gt_data), check_replan_efficiency(result, gt_data)]
    result.metrics = metrics
    result.total_metrics = len(metrics)
    result.passed_metrics = len([m for m in metrics if m.pass_])

def _append_output_contract(result: AgentRunResult, task_type: str, gt_data: dict) -> None:
    """Keep a missing schema field from being converted into a skipped pass."""
    if task_type in {'multi_entity', 'itinerary'}:
        return
    if task_type == 'safety_planning' and isinstance(gt_data.get('safety_choice'), dict):
        return
    if task_type == 'region_aware' and isinstance(gt_data.get('region_decision'), dict):
        return
    if task_type not in {'multi_entity', 'itinerary', 'safety_planning', 'region_aware', 'dynamic_replanning'}:
        return
    result.metrics.insert(0, check_required_structured_output(result, task_type, gt_data))
    result.total_metrics = len(result.metrics)
    result.passed_metrics = len([metric for metric in result.metrics if metric.pass_])

def _result_to_dict(result: AgentRunResult) -> dict[str, Any]:
    return {'task_id': result.task_id, 'dynamic_event_mode': result.dynamic_event_mode, 'dynamic_information_mode': result.dynamic_information_mode, 'explicit_update_deliveries': result.explicit_update_deliveries, 'task_folder': getattr(result, 'task_folder', ''), 'task_type': getattr(result, 'task_type', ''), 'source_batch_id': getattr(result, 'source_batch_id', ''), 'source_tasks_dir': getattr(result, 'source_tasks_dir', ''), 'user_prompt': result.user_prompt, 'grade': result.grade, 'infrastructure_failure': result.grade == 'INFRASTRUCTURE_FAILURE', 'total_score': result.total_score, 'passed': result.passed_metrics, 'total': result.total_metrics, 'duration_s': result.duration_s, 'tokens_used': result.tokens_used, 'model_name': result.model_name, 'evaluation_world_snapshot': result.evaluation_world_snapshot, 'termination': result.termination, 'agent_protocol': result.agent_protocol, 'recovery_checkpoint': result.recovery_checkpoint, 'input_modalities': result.input_modalities, 'input_asset_hashes': result.input_asset_hashes, 'metrics': [{'metric': m.metric, 'label': m.label, 'pass': m.pass_, 'detail': m.detail, **({'verification_status': m.verification_status} if m.verification_status else {})} for m in result.metrics], 'final_answer': result.final_answer, 'initial_plan': result.initial_plan, 'gt_route_id': result.gt_route_id, 'gt_leg_route_ids': getattr(result, 'gt_leg_route_ids', []), 'gt_route_summary': result.gt_route_summary, 'core_places': result.core_places, 'dynamic_event_states': result.dynamic_event_states, 'dynamic_causal_log': result.dynamic_causal_log, **({'answer_versions': result.answer_versions} if result.answer_versions or getattr(result, 'task_type', '') == 'dynamic_replanning' else {}), 'trace': [{'seq': t.seq, 'timestamp': t.timestamp, 'tool': t.tool, 'args': t.args, 'result': _summarize_result(t.tool, t.result), 'raw_result': t.result, 'duration_ms': t.duration_ms, 'llm_input': t.llm_input, 'llm_output': t.llm_output} for t in result.trace]}

def _summarize_result(tool: str, result: dict) -> dict:
    if tool == 'search_places':
        return {'count': result.get('count', 0), 'names': [r.get('name', '') for r in result.get('results', [])[:5]]}
    elif tool == 'query_routes':
        routes = result.get('routes', [])
        return {'count': len(routes), 'summary': [{'route_id': r.get('route_id', '')[:16], 'mode': r.get('mode', ''), 'distance_m': r.get('distance_m', 0), 'duration_s': r.get('duration_s', 0)} for r in routes]}
    elif tool == 'get_place_detail':
        return {'found': result.get('found', False), 'name': result.get('name', '')}
    elif tool == 'final_answer':
        return {'status': result.get('status', ''), 'route_count': result.get('route_count', 0)}
    return result

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Run route planning benchmark tests.')
    parser.add_argument('--task', default='', help='Path to a single task folder')
    parser.add_argument('--tasks', default='', help='Path to tasks directory (batch run)')
    parser.add_argument('--routes', default='', help='Path to route_cache.json (auto-discover if omitted)')
    parser.add_argument('--world', default='', help='Path to world.json for place_grounding/place_discovery')
    parser.add_argument('--model', default='cliproxy_gpt55_pro', help='Model endpoint key (default: cliproxy_gpt55_pro)')
    parser.add_argument('--output', default='', help='Custom output JSON path (optional)')
    parser.add_argument('--dynamic-event-mode', choices=('shared_world', 'task_only'), default='shared_world')
    parser.add_argument('--dynamic-information-mode', choices=('pull', 'explicit_update'), default='pull')
    args = parser.parse_args()
    if args.task:
        cache = _resolve_route_cache(args.routes or None, Path(args.task).parent)
        result = run_task(args.task, route_cache_path=cache, model_key=args.model, world_path=args.world, dynamic_event_mode=args.dynamic_event_mode, dynamic_information_mode=args.dynamic_information_mode)
        print(f"\n{'=' * 50}")
        print(f'Task: {result.task_id}')
        print(f'Score: {result.passed_metrics}/{result.total_metrics} ({result.total_score}%) — {result.grade}')
        print(f"Time: {result.duration_s}s, Tokens: {result.tokens_used.get('total', 0)}")
        print(f"{'=' * 50}")
        for m in result.metrics:
            print(f"  {('PASS' if m.pass_ else 'FAIL')} {m.label}: {m.detail}")
        return 0
    if args.tasks:
        run_all_tasks(args.tasks, route_cache_path=args.routes, model_key=args.model, output_path=args.output or None, world_path=args.world, dynamic_event_mode=args.dynamic_event_mode, dynamic_information_mode=args.dynamic_information_mode)
        return 0
    print('Usage: python -m test_runner.runner --task <path> or --tasks <path>', file=sys.stderr)
    return 1
if __name__ == '__main__':
    raise SystemExit(main())
