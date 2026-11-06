"""Restart-safe continuation of existing evaluation histories, without changing Agent/Judge.

Explicit CLI launch only: python -m test_runner.resume --plan <prepared-plan.json>
A wave barrier limits execution to two runs; completed model failures are never retried.
"""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from shared.io import read_json, write_json


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_text(path, text):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def identity(row):
    return str((Path(row['source_tasks_dir']) / row['task_folder']).resolve())


def is_api_failure(row):
    term = row.get('termination') or {}
    return term.get('reason') == 'evaluation_infrastructure_failure' and term.get('category') == 'model_api'


def load_rows(run_dir):
    """Recover detail-first commits; refuse conflicting evidence instead of picking a winner."""
    run_dir = Path(run_dir)
    rows, files = {}, {}
    for p in sorted((run_dir / 'tasks').glob('*.json')):
        if p.name == 'task_index.json':
            continue
        row = read_json(p)
        key = identity(row)
        if key in rows:
            raise ValueError('Duplicate task detail: ' + key)
        rows[key], files[key] = row, p.name
    path = run_dir / 'results.jsonl'
    if path.exists():
        lines = path.read_text().splitlines()
        seen = set()
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                if i == len(lines) - 1:
                    continue  # crashed final append; committed detail remains authoritative
                raise
            key = identity(row)
            if key in seen:
                raise ValueError('Duplicate JSONL result: ' + key)
            seen.add(key)
            if key in rows and rows[key] != row:
                raise ValueError('Conflicting detail/JSONL: ' + key)
            rows[key] = row
    summary = run_dir / 'results.json'
    if not rows and summary.exists():
        for row in read_json(summary).get('tasks', []):
            key = identity(row)
            if key in rows:
                raise ValueError('Duplicate summary result: ' + key)
            rows[key] = row
    return rows, files


def selection(manifest):
    records = manifest.get('selected_tasks') or manifest.get('scope', {}).get('selected_tasks') or []
    keys = [str(Path(x['task_folder']).resolve()) for x in records]
    if not keys or len(keys) != len(set(keys)):
        raise ValueError('Missing or duplicated original task selection')
    return keys


def reconcile(run_dir, rows, files, *, status, error=None):
    """Publish all frontend representations from identical retained result objects."""
    run_dir = Path(run_dir)
    manifest = read_json(run_dir / 'run_manifest.json')
    keys = selection(manifest)
    from app.runtime.shared_events import validate_event_mode
    mode = validate_event_mode(manifest.get('dynamic_event_mode', 'shared_world'))
    if any(row.get('dynamic_event_mode', 'shared_world') != mode for row in rows.values()):
        raise ValueError('Result event mode differs from original run; refusing mixed-mode results')
    if set(rows) - set(keys):
        raise ValueError('Result outside original selection')
    ordered = [rows[k] for k in keys if k in rows]
    used = {p.name for p in (run_dir / 'tasks').glob('*.json')}
    next_id = max([int(Path(n).stem) for n in used if Path(n).stem.isdigit()] or [0]) + 1
    for key in keys:
        if key not in rows:
            continue
        if key not in files:
            while f'{next_id:04d}.json' in used:
                next_id += 1
            files[key] = f'{next_id:04d}.json'
            used.add(files[key]); next_id += 1
        path = run_dir / 'tasks' / files[key]
        if not path.exists():
            write_json(path, rows[key])
        elif read_json(path) != rows[key]:
            raise ValueError('Refusing to overwrite existing result: ' + str(path))
    entries = [{'task_id': rows[k]['task_id'], 'task_type': rows[k]['task_type'], 'file_name': files[k]}
               for k in keys if k in rows]
    write_json(run_dir / 'tasks/task_index.json', {'schema_version': 'geoword.evaluation_task_index.v1', 'entries': entries})
    atomic_text(run_dir / 'results.jsonl', ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in ordered))
    tokens = {k: sum((r.get('tokens_used') or {}).get(k, 0) for r in ordered) for k in ('prompt', 'completion', 'total')}
    grades = {}
    by_type = {}
    for r in ordered:
        grade = r['grade']; grades[grade] = grades.get(grade, 0) + 1
        t = by_type.setdefault(r['task_type'], {'task_count': 0, 'passed': 0, 'token_usage': {k: 0 for k in tokens}})
        t['task_count'] += 1; t['passed'] += int(grade == 'PASS')
        for k in tokens:
            t['token_usage'][k] += (r.get('tokens_used') or {}).get(k, 0)
    state = read_json(run_dir / 'run_state.json')
    state.pop('last_result', None)
    state.update(status=status, phase=status, completed_count=len(rows), total_count=len(keys),
                 passed=grades.get('PASS', 0), infrastructure_failed=grades.get('INFRASTRUCTURE_FAILURE', 0),
                 failed=len(rows)-grades.get('PASS', 0)-grades.get('INFRASTRUCTURE_FAILURE', 0),
                 token_usage=tokens, updated_at=now(), error=error)
    if status in ('completed', 'failed', 'interrupted'):
        state['finished_at'] = now()
    else:
        state['finished_at'] = None
    summary = {'run_id': run_dir.name, 'model_key': manifest['model_key'],
               'dynamic_event_mode': manifest.get('dynamic_event_mode', 'shared_world'),
               'model_name': ordered[0].get('model_name', '') if ordered else '',
               'world_id': manifest.get('scope', {}).get('world_id', ''),
               'city_id': manifest.get('scope', {}).get('city', ''),
               'scope': manifest.get('scope', {}), 'started_at': state.get('started_at'),
               'finished_at': state.get('finished_at'), 'task_count': len(keys),
               'completed_count': len(rows), 'passed': state['passed'], 'failed': state['failed'],
               'infrastructure_failed': state['infrastructure_failed'], 'grades': grades,
               'token_usage': tokens, 'total_duration_s': sum(r.get('duration_s', 0) for r in ordered),
               'tasks_by_type': by_type, 'tasks': ordered}
    write_json(run_dir / 'results.json', summary)
    write_json(run_dir / 'run_state.json', state)
    return state


def process_identity(pid=None):
    pid = pid or os.getpid()
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
                return None
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
                return None
            created = values[0].dwLowDateTime | (values[0].dwHighDateTime << 32)
            return {'pid': pid, 'start_ticks': created, 'boot_id': 'windows-filetime'}
        finally:
            kernel.CloseHandle(handle)
    try:
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if stat[0] == 'Z':
            return None
        return {'pid': pid, 'start_ticks': stat[19], 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError, IndexError):
        return None


def managed_worker_alive(state):
    expected = state.get('resume_worker')
    return bool(expected and process_identity(expected.get('pid')) == expected)


@contextlib.contextmanager
def exclusive(path):
    if os.name == 'nt':
        import msvcrt
        with open(path, 'a+b') as handle:
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BlockingIOError('Another recovery worker owns this run.') from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    with open(path, 'a') as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def endpoint_contract(config, key):
    from shared.config_loader import PipelineConfig
    ep = dict(PipelineConfig(config).model_endpoint(key))
    ep.pop('api_key', None)
    return ep


def verify_plan(plan):
    for path, expected in plan['input_hashes'].items():
        if digest(path) != expected:
            raise ValueError('Frozen input/code changed since preparation: ' + path)
    for item in plan['runs']:
        m = read_json(Path(item['run_dir']) / 'run_manifest.json')
        if endpoint_contract(m.get('config_yaml', 'config.yaml'), m['model_key']) != plan['endpoint']:
            raise ValueError('Model configuration changed')
        rows, _ = load_rows(item['run_dir'])
        if set(rows) - set(selection(m)):
            raise ValueError('Unknown result identity')


def resume_run(run_dir):
    from test_runner.runner import run_task, _resolve_route_cache, _result_to_dict
    from test_runner.verification.shared.formalization import is_formal_task
    run_dir = Path(run_dir)
    with exclusive(run_dir / '.resume.lock'):
        manifest = read_json(run_dir / 'run_manifest.json')
        rows, files = load_rows(run_dir)
        state = read_json(run_dir / 'run_state.json')
        state.update(resume_worker=process_identity(), resume_started_at=now(), status='running', phase='running')
        write_json(run_dir / 'run_state.json', state)
        started = time.monotonic(); previous_elapsed = state.get('elapsed_seconds', 0)
        try:
            reconcile(run_dir, rows, files, status='running')
            for key in selection(manifest):
                if key in rows:
                    continue
                folder = Path(key)
                if not is_formal_task(folder):
                    raise ValueError('Task is no longer formal: ' + key)
                task = read_json(folder / 'task.json')
                state = read_json(run_dir / 'run_state.json')
                state.update(current_task_id=task['task_id'], updated_at=now())
                write_json(run_dir / 'run_state.json', state)
                cache = '' if task['task_type'] in ('place_grounding', 'place_discovery') else _resolve_route_cache('', folder.parent)
                result = run_task(folder, route_cache_path=cache, model_key=manifest['model_key'],
                                  config_yaml=manifest.get('config_yaml', 'config.yaml'), world_path=manifest.get('world_path', ''),
                                  dynamic_event_mode=manifest.get('dynamic_event_mode', 'shared_world'))
                result.source_tasks_dir = str(folder.parent)
                result.source_batch_id = folder.parent.parent.name
                row = _result_to_dict(result)
                if identity(row) != key:
                    raise ValueError('Result identity mismatch')
                rows[key] = row
                state = read_json(run_dir / 'run_state.json')
                state['elapsed_seconds'] = previous_elapsed + time.monotonic() - started
                write_json(run_dir / 'run_state.json', state)
                reconcile(run_dir, rows, files, status='running')
            reconcile(run_dir, rows, files, status='completed')
        except Exception as exc:
            reconcile(run_dir, rows, files, status='failed', error=str(exc))
            raise


def execute_waves(waves, start, wait):
    """Wait for the entire pair, not just one free slot, before the next wave."""
    for wave in waves:
        if not 1 <= len(wave) <= 2:
            raise ValueError('Wave size must be one or two')
        handles = [start(run) for run in wave]
        codes = [wait(handle) for handle in handles]
        if any(code != 0 for code in codes):
            raise RuntimeError('A resume worker failed; remaining waves were not started')


def run_queue(plan_path):
    plan_path = Path(plan_path).resolve()
    with exclusive(plan_path.with_suffix('.lock')):
        plan = read_json(plan_path)
        verify_plan(plan)
        status_path = plan_path.with_name('queue_state.json')
        # Live queue ownership lets the frontend distinguish queued runs from stale threads.
        for item in plan['runs']:
            p = Path(item['run_dir']) / 'run_state.json'; state = read_json(p)
            if managed_worker_alive(state):
                raise ValueError('Run already has a live owner')
        for item in plan['runs']:
            p = Path(item['run_dir']) / 'run_state.json'; state = read_json(p)
            if state.get('status') != 'completed':
                state.update(status='queued', phase='queued', resume_worker=process_identity())
                write_json(p, state)
        write_json(status_path, {'status': 'running', 'worker': process_identity(), 'started_at': now()})
        processes = []
        def start(run_dir):
            log = open(Path(run_dir)/'resume.log', 'a')
            try:
                p = subprocess.Popen([sys.executable, '-m', 'test_runner.resume', '--run', run_dir], stdout=log, stderr=subprocess.STDOUT)
            finally:
                log.close()
            processes.append(p)
            return p
        try:
            execute_waves(plan['waves'], start, lambda p: p.wait())
            write_json(status_path, {'status': 'completed', 'finished_at': now()})
        except BaseException:
            # Do not orphan children or let a subsequent queue duplicate their work.
            for p in processes:
                if p.poll() is None:
                    p.terminate()
            for p in processes:
                p.wait()
            write_json(status_path, {'status': 'interrupted', 'finished_at': now()})
            raise


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--plan'); group.add_argument('--run'); group.add_argument('--check-plan')
    args = parser.parse_args()
    import signal
    def stop(signum, frame):
        raise KeyboardInterrupt("Termination requested")
    signal.signal(signal.SIGTERM, stop)
    if args.check_plan:
        verify_plan(read_json(args.check_plan)); print('Prepared plan verified; no model calls.')
    elif args.plan:
        run_queue(args.plan)
    else:
        resume_run(args.run)

if __name__ == '__main__':
    main()
