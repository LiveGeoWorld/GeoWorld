"""Generation boundary and saved-job recovery without provider calls."""
import json
from types import SimpleNamespace

import pytest

from vis.tasks import generation_http as module
from task_module.config import TaskGeneratorConfig
from task_module.generator import TaskGenerator


def test_unmigrated_generation_is_rejected_before_world_or_provider_access(tmp_path):
    api = module.GenerationHTTP(SimpleNamespace(), tmp_path, {'city': 'test_city'})
    api._read_json_body = lambda: {'task_type': 'dynamic_replanning'}
    errors = []
    api._error = lambda message, status: errors.append((message, status))
    api.handle('POST', '/api/generate')
    assert errors == [('Pending migration and integration for this task type.', 501)]
    with pytest.raises(NotImplementedError, match='dynamic_replanning'):
        TaskGenerator(TaskGeneratorConfig(task_type_counts={'dynamic_replanning': 1})).generate()


def test_job_status_cannot_escape_job_directory(tmp_path):
    api = module.GenerationHTTP(SimpleNamespace(), tmp_path, {'city': 'test_city', 'job_id': '../outside'})
    errors = []
    api._error = lambda message, status: errors.append(status)
    api.handle('GET', '/api/generate/status')
    assert errors == [400]


def test_completed_job_survives_empty_in_memory_registry(tmp_path, monkeypatch):
    record = {'job_id': 'gen_saved', 'status': 'completed', 'completed_count': 1}
    (tmp_path / 'gen_saved.json').write_text(json.dumps(record), encoding='utf-8')
    monkeypatch.setattr(module, '_GENERATION_JOBS', {})
    monkeypatch.setattr(module, '_generation_jobs_root', lambda: tmp_path)
    assert module._load_generation_job('gen_saved') == record


def test_append_counts_only_formal_valid_tasks(tmp_path):
    tasks = tmp_path / 'tasks'
    for name, valid, state in [('formal', True, 'formal'), ('draft', True, 'draft'), ('bad', False, 'formal')]:
        folder = tasks / name
        folder.mkdir(parents=True)
        (folder / 'task.json').write_text('{}', encoding='utf-8')
        (folder / 'task_validation.json').write_text(json.dumps({'valid': valid, 'state': state}), encoding='utf-8')
    assert module._generation_target_count(tmp_path, 'place_grounding', 'append', 2) == 3
    assert module._generation_target_count(tmp_path, 'place_grounding', 'new', 2) == 2

def test_safety_route_resources_use_the_registered_inherited_cache(tmp_path, monkeypatch):
    from task_module.task_types.safety_planning import generation_inputs

    route = tmp_path / 'routes' / 'v2' / 'route_cache.json'
    route.parent.mkdir(parents=True)
    route.write_text(json.dumps({'cache': []}), encoding='utf8')
    api = object.__new__(module.GenerationHTTP)
    api.params = {}
    api._current_browsing_world_id = lambda: 'world'
    api._current_world_cities = lambda: [{'city_id': 'city', 'active_version': 3, 'label': 'City'}]
    api._route_city_world = lambda *_: {'places': []}
    api._route_city_characters = lambda *_: {'characters': [{}]}
    monkeypatch.setattr(generation_inputs, 'resolve_assets', lambda *_: {'route_cache': str(route)})
    monkeypatch.setattr(module, 'resolve_world_route_cache',
                        lambda *_: pytest.fail('Safety must use its registered route asset'))
    seen = []
    monkeypatch.setattr(module, '_multi_entity_effective_route_payload',
                        lambda path: seen.append(path) or {'cached_pair_count': 12})
    result = []
    api._json = result.append
    api.params['city'] = 'city'
    api.handle('GET', '/api/safety/route-resources')
    assert seen == [route]
    assert result[0]['active_version'] == 3
    assert result[0]['cached_route_facts'] == 12


def test_cancel_running_safety_generation_preserves_saved_tasks(tmp_path, monkeypatch):
    saved = tmp_path / 'tasks' / 'task_0001'
    saved.mkdir(parents=True)
    record = saved / 'task.json'
    record.write_text('{"task_type":"safety_planning"}', encoding='utf8')
    api = object.__new__(module.GenerationHTTP)
    api._read_json_body = lambda: {'job_id': 'safety-job'}
    result = []
    api._json = result.append
    job = {'job_id': 'safety-job', 'status': 'running', 'task_type': 'safety_planning'}
    stopped = []
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: stopped.append(True))
    monkeypatch.setattr(module, '_GENERATION_JOBS', {'safety-job': job})
    monkeypatch.setattr(module, '_GENERATION_PROCESSES', {'safety-job': process})
    monkeypatch.setattr(module, '_update_generation_job', lambda job, **fields: job.update(fields))
    monkeypatch.setattr(module, '_generation_snapshot', dict)

    api.handle('POST', '/api/generate/cancel')

    assert stopped == [True]
    assert result[0]['job']['status'] == 'cancelling'
    assert json.loads(record.read_text(encoding='utf8'))['task_type'] == 'safety_planning'
