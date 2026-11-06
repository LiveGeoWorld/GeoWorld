"""Generation boundary and saved-job recovery without provider calls."""
import json
from types import SimpleNamespace

import pytest

from vis.tasks import generation_http as module
from task_module.config import TaskGeneratorConfig
from task_module.generator import TaskGenerator


def test_unmigrated_generation_is_rejected_before_world_or_provider_access(tmp_path):
    api = module.GenerationHTTP(SimpleNamespace(), tmp_path, {'city': 'test_city'})
    api._read_json_body = lambda: {'task_type': 'safety_planning'}
    errors = []
    api._error = lambda message, status: errors.append((message, status))
    api.handle('POST', '/api/generate')
    assert errors == [('Pending migration and integration for this task type.', 501)]
    with pytest.raises(NotImplementedError, match='safety_planning'):
        TaskGenerator(TaskGeneratorConfig(task_type_counts={'safety_planning': 1})).generate()


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
