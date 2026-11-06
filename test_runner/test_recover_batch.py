import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_runner import recover_batch as batch
from test_runner.agent.test_react_protocol import _agent_with_response


def test_atomic_and_source_change(tmp_path):
    path = tmp_path / 'results.json'
    batch.atomic(path, {'tasks': [{'task_id': 't'}]})
    item = {'results': str(path), 'source_sha256': batch.digest(path), 'task_id': 't'}
    assert batch.original(item)['task_id'] == 't'
    batch.atomic(path, {'tasks': []})
    with pytest.raises(ValueError, match='changed'):
        batch.original(item)


def test_checkpoint_feedback_and_inflight_are_distinct():
    agent = _agent_with_response({'choices': [{'message': {'content': 'bad'}}]})
    agent.max_steps = 2
    agent._max_protocol_retries = 2
    saved = []
    agent.run(checkpoint_sink=lambda c: saved.append(json.loads(json.dumps(c))))
    assert saved[0]['safe_to_resume']
    assert any(s['phase'] == 'api_inflight' and not s['safe_to_resume'] for s in saved)
    boundary = [s for s in saved if s['phase'] == 'boundary' and s['row']['recovery_checkpoint']['model_steps']]
    assert boundary
    assert boundary[0]['row']['recovery_checkpoint']['protocol_errors'][0]['retry_allowed']
    assert 'call_rejected' in boundary[0]['row']['recovery_checkpoint']['messages'][-1]['content']


def test_worker_refuses_uncertain_interruption(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, 'original', lambda item: {})
    batch.atomic(tmp_path / 't/checkpoint.json', {'safe_to_resume': False})
    monkeypatch.setattr(batch, 'invoke', lambda *a: pytest.fail('Must not call Agent'))
    result = batch.worker({'key': 't'}, 'config', tmp_path)
    assert result['state'] == 'blocked'
    assert 'manual review' in result['error']


def test_batch_skips_final_fail_and_blocked(tmp_path, monkeypatch):
    items = [{'key': key, 'model_name': 'm', 'preflight_error': None} for key in ['done', 'todo', 'blocked']]
    items[2]['preflight_error'] = 'Missing snapshot'
    batch.atomic(tmp_path / 'plan.json', {'schema_version': 'geoworld.recovery_batch.v1', 'config': 'c', 'items': items})
    batch.atomic(tmp_path / 'done/result.json', {'result': {'grade': 'FAIL'}})
    called = []
    def fake(item, *args):
        called.append(item['key'])
        return {'state': 'completed', 'grade': 'PASS'}
    monkeypatch.setattr(batch, 'ProcessPoolExecutor', ThreadPoolExecutor)
    monkeypatch.setattr(batch, 'worker', fake)
    batch.run(tmp_path / 'plan.json', workers=2)
    assert called == ['todo']
    assert batch.read(tmp_path / 'summary.json')['counts'] == {'completed': 2, 'blocked': 1}
    assert not (tmp_path / '.running.lock').exists()


def test_batch_lock_prevents_duplicate_launch(tmp_path):
    (tmp_path / '.running.lock').write_text('123')
    with pytest.raises(FileExistsError):
        batch.run(tmp_path / 'plan.json')


def test_prepare_freezes_explicit_runs_without_model_calls(tmp_path, monkeypatch):
    from shared.config_loader import PipelineConfig
    source = tmp_path / 'old.json'
    batch.atomic(source, {'run_id': 'r', 'tasks': [
        {'task_id': 't', 'model_name': 'm', 'termination': {'category': 'no_action'},
         'task_folder': '/old/outputs/tasks/t'},
        {'task_id': 'completed', 'termination': {}}]})
    selection = tmp_path / 'selection.json'
    batch.atomic(selection, {'runs': [{'results': str(source), 'model_key': 'key', 'task_root': str(tmp_path)}]})
    monkeypatch.setattr(PipelineConfig, '__init__', lambda *a: None)
    monkeypatch.setattr(PipelineConfig, 'model_endpoint', lambda *a: {'model': 'm'})
    monkeypatch.setattr(batch, 'preflight', lambda *a: None)
    plan = batch.prepare(selection, 'config', tmp_path / 'new')
    assert len(plan['items']) == 1
    assert plan['items'][0]['task_dir'] == str(tmp_path / 'outputs/tasks/t')
    assert plan['items'][0]['source_sha256'] == batch.digest(source)
    with pytest.raises(ValueError, match='replace'):
        batch.prepare(selection, 'config', tmp_path / 'new')


def test_real_process_worker_records_blocker_without_api(tmp_path):
    item = {'key': 't', 'model_name': 'm', 'preflight_error': None,
            'results': str(tmp_path / 'missing.json'), 'source_sha256': 'absent'}
    batch.atomic(tmp_path / 'plan.json', {'schema_version': 'geoworld.recovery_batch.v1',
                                        'config': 'unused', 'items': [item]})
    batch.run(tmp_path / 'plan.json', workers=1)
    assert batch.read(tmp_path / 't/status.json')['state'] == 'blocked'
    assert batch.read(tmp_path / 'summary.json')['counts'] == {'blocked': 1}
