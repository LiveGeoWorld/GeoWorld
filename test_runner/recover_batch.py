"""Explicit frozen selection -> audited, process-isolated failed-turn recovery.

Selection JSON: {"runs": [{"results": "absolute/results.json",
"model_key": "configured_key", "task_root": "local/repository",
"historical_revision": "optional_verified_revision"}]}.
No automatic latest-run discovery, world-hash waiver, or model-failure retry.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from unittest.mock import patch

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    (fd, temporary) = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

class Ready(Exception):
    pass

def original(item):
    if digest(item['results']) != item['source_sha256']:
        raise ValueError('Source results changed after selection was frozen')
    source = read(item['results'])
    rows = [r for r in source['tasks'] if r['task_id'] == item['task_id']]
    if len(rows) != 1:
        raise ValueError('Expected exactly one original task')
    row = rows[0]
    if item.get('historical_revision'):
        row['recovery_input'] = {'historical_revision': item['historical_revision'], 'archived_prompt_evidence': list({(t.get('llm_input') or {}).get('system_prompt') for r in source['tasks'] for t in r.get('trace', []) if (t.get('llm_input') or {}).get('system_prompt')})}
    return row

def invoke(item, row, config, sink=None):
    from test_runner.runner import run_task, _resolve_route_cache
    folder = Path(item['task_dir'])
    return run_task(folder, route_cache_path=_resolve_route_cache('', folder.parent), model_key=item['model_key'], config_yaml=config, dynamic_event_mode=row.get('dynamic_event_mode', 'shared_world'), recovery_record=row, checkpoint_sink=sink)

def preflight(item, config):
    from test_runner.agent.llm_agent import LLMAgent
    try:
        with patch.object(LLMAgent, '_call_llm', side_effect=Ready()):
            invoke(item, original(item), config)
        return None
    except Ready:
        return None
    except Exception as exc:
        return str(exc)

def prepare(selection, config, output):
    from shared.config_loader import PipelineConfig
    output = Path(output).resolve()
    if (output / 'plan.json').exists():
        raise ValueError('Refusing to replace a frozen plan')
    config = str(Path(config).resolve())
    selected = read(selection)
    (items, seen) = ([], set())
    for run in selected['runs']:
        path = str(Path(run['results']).resolve())
        source = read(path)
        endpoint = PipelineConfig(config).model_endpoint(run['model_key'])
        for row in source['tasks']:
            if (row.get('termination') or {}).get('category') not in {'no_action', 'invalid_action', 'model_api'}:
                continue
            identity = (path, row['task_id'])
            if identity in seen:
                raise ValueError('Duplicate source task selection')
            seen.add(identity)
            key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
            folder = (run.get('task_dirs') or {}).get(row['task_id'])
            if not folder:
                saved = row.get('task_folder', '').replace('\\', '/')
                if '/outputs/' not in saved:
                    saved = row.get('source_tasks_dir', '').replace('\\', '/').rstrip('/') + '/' + saved
                if '/outputs/' not in saved or not run.get('task_root'):
                    raise ValueError('Supply task_root or explicit task_dirs mapping')
                folder = str(Path(run['task_root']) / 'outputs' / saved.split('/outputs/', 1)[1])
            item = {'key': key, 'results': path, 'source_sha256': digest(path), 'source_run_id': source.get('run_id'), 'task_id': row['task_id'], 'model_key': run['model_key'], 'model_name': row.get('model_name'), 'task_dir': str(Path(folder).resolve()), 'dynamic_event_mode': row.get('dynamic_event_mode', 'shared_world'), 'historical_revision': run.get('historical_revision')}
            item['original_tokens'] = row.get('tokens_used') or {}
            item['original_duration_s'] = row.get('duration_s', 0)
            item['original_failed_step'] = (row.get('agent_protocol') or {}).get('step')
            error = 'Configured model differs from original model' if endpoint['model'] != row.get('model_name') else preflight(item, config)
            item['preflight_error'] = error
            items.append(item)
    plan = {'schema_version': 'geoworld.recovery_batch.v1', 'config': config, 'selection_sha256': digest(selection), 'items': items}
    atomic(output / 'plan.json', plan)
    atomic(output / 'summary.json', {'selected': len(items), 'ready': sum((not i['preflight_error'] for i in items)), 'blocked': sum((bool(i['preflight_error']) for i in items))})
    return plan

def worker(item, config, root):
    from test_runner.runner import _result_to_dict
    from test_runner.recovery_audit import event
    from uuid import uuid4
    attempt_id = uuid4().hex
    root = Path(root) / item['key']
    root.mkdir(parents=True, exist_ok=True)
    (status_path, checkpoint_path) = (root / 'status.json', root / 'checkpoint.json')
    result_path = root / 'result.json'
    context = {'attempt_id': attempt_id, 'task_id': item.get('task_id', item['key']), 'source_run_id': item.get('source_run_id'), 'model_name': item.get('model_name'), 'dynamic_event_mode': item.get('dynamic_event_mode', 'shared_world')}
    if (root.parent / 'plan.json').exists():
        context['plan_sha256'] = digest(root.parent / 'plan.json')
    event(root / 'audit', 'recovery_started', **context)
    try:
        row = original(item)
        parent = None
        if checkpoint_path.exists():
            saved = read(checkpoint_path)
            parent = saved.get('attempt_id')
            if not saved['safe_to_resume']:
                raise ValueError('Interrupted in-flight request/tool/terminal grading; manual review required')
            row.update(saved['row'])
        event(root / 'audit', 'recovery_input_loaded', **context, parent_attempt_id=parent, source_sha256=item['source_sha256'], model_steps=(row.get('recovery_checkpoint') or {}).get('model_steps'), prior_tool_calls=len(row.get('trace') or []), frozen_binding=row.get('evaluation_world_snapshot'), prior_tokens=row.get('tokens_used'))

        def sink(value):
            if value['phase'] == 'api_attempt':
                event(root / 'audit', 'api_attempt', **context, model_step=value['model_step'], call_round=value['call_round'], attempt=value['attempt'])
                return
            value['attempt_id'] = attempt_id
            atomic(checkpoint_path, value)
            checkpoint = value['row']['recovery_checkpoint']
            event(root / 'audit', 'checkpoint_saved', **context, phase=value['phase'], safe_to_resume=value['safe_to_resume'], model_steps=checkpoint['model_steps'], round_retries=checkpoint.get('round_retries'), tool_calls=len(value['row']['trace']), tokens=value['row']['tokens_used'])
        result = invoke(item, row, config, sink)
        payload = {'source': item, 'attempt_id': attempt_id, 'parent_attempt_id': parent, 'result': _result_to_dict(result)}
        attempt = len(list(root.glob('attempt_*.json'))) + 1
        atomic(root / f'attempt_{attempt:04d}.json', payload)
        atomic(result_path, payload)
        status = {'state': 'infrastructure_failure' if result.grade == 'INFRASTRUCTURE_FAILURE' else 'completed', 'grade': result.grade, 'tokens': result.tokens_used, 'duration_s': result.duration_s, 'model_steps': result.agent_protocol.get('model_steps'), 'correction_count': sum((bool(e.get('retry_allowed')) for e in result.agent_protocol.get('errors', []))), 'attempt_id': attempt_id}
        if status['state'] == 'infrastructure_failure':
            atomic(checkpoint_path, {'safe_to_resume': True, 'phase': 'api_failed', 'attempt_id': attempt_id, 'row': payload['result']})
    except Exception as exc:
        status = {'state': 'blocked', 'error': str(exc)}
    atomic(status_path, status)
    event(root / 'audit', 'recovery_finished', **context, **status)
    return status

def run(plan_path, workers=5, per_model=1):
    from test_runner.recovery_audit import event
    if workers < 1 or per_model < 1:
        raise ValueError('Concurrency limits must be positive')
    plan_path = Path(plan_path).resolve()
    root = plan_path.parent
    lock = root / '.running.lock'
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    try:
        event(root / 'audit', 'batch_started', plan_sha256=digest(plan_path), workers=workers, per_model=per_model, pid=os.getpid())
        plan = read(plan_path)
        if plan.get('schema_version') != 'geoworld.recovery_batch.v1':
            raise ValueError('Unsupported plan schema')
        (states, pending) = ({}, [])
        for item in plan['items']:
            folder = root / item['key']
            status = read(folder / 'status.json') if (folder / 'status.json').exists() else {}
            if (folder / 'result.json').exists():
                saved_result = read(folder / 'result.json')['result']
                grade = saved_result['grade']
                if grade in {'PASS', 'FAIL'}:
                    status.update(state='completed', grade=grade, tokens=saved_result.get('tokens_used'), duration_s=saved_result.get('duration_s'))
                    event(root / 'audit', 'completed_task_skipped', task_key=item['key'], grade=grade)
            if item['preflight_error']:
                status = {'state': 'blocked', 'error': item['preflight_error']}
            states[item['key']] = status or {'state': 'pending'}
            if states[item['key']]['state'] not in {'completed', 'blocked'}:
                pending.append(item)

        def summary():
            models = {}
            for item in plan['items']:
                models.setdefault(item['model_name'], Counter()).update([states[item['key']]['state']])
            atomic(root / 'summary.json', {'selected': len(states), 'counts': dict(Counter((s['state'] for s in states.values()))), 'per_model': {m: dict(counts) for (m, counts) in models.items()}, 'grades': dict(Counter((s.get('grade') for s in states.values() if s.get('grade')))), 'tasks': states})
        (active, model_counts) = ({}, Counter())
        with ProcessPoolExecutor(max_workers=workers) as pool:
            while pending or active:
                for item in list(pending):
                    model = item['model_name']
                    if len(active) >= workers:
                        break
                    if model_counts[model] >= per_model:
                        continue
                    pending.remove(item)
                    active[pool.submit(worker, item, plan['config'], str(root))] = item
                    model_counts[model] += 1
                    states[item['key']] = {'state': 'running'}
                    event(root / 'audit', 'task_scheduled', task_key=item['key'], model_name=model)
                summary()
                if active:
                    (done, _) = wait(active, return_when=FIRST_COMPLETED)
                    for future in done:
                        item = active.pop(future)
                        model_counts[item['model_name']] -= 1
                        try:
                            states[item['key']] = future.result()
                        except Exception as exc:
                            states[item['key']] = {'state': 'blocked', 'error': str(exc)}
                            atomic(root / item['key'] / 'status.json', states[item['key']])
            summary()
    finally:
        event(root / 'audit', 'batch_stopped', pid=os.getpid())
        lock.unlink()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--selection', required=True)
    prep.add_argument('--config', default='config.yaml')
    prep.add_argument('--output', required=True)
    execute = sub.add_parser('run')
    execute.add_argument('--plan', required=True)
    execute.add_argument('--workers', type=int, default=5)
    execute.add_argument('--per-model', type=int, default=1)
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare(args.selection, args.config, args.output)
    else:
        run(args.plan, args.workers, args.per_model)
if __name__ == '__main__':
    main()
