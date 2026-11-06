import json
import pytest
from vis.tasks.app_content import preview, catalog, resolve_folder

def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf8')

def fixture(tmp_path, monkeypatch, dynamic=True):
    import app.runtime.episode as runner
    kind = 'dynamic_replanning' if dynamic else 'safety_planning'
    folder = tmp_path / kind / 'world_test/city/batch/tasks/task_1'
    put(folder / 'task.json', {'task_type': kind, 'task_id': 'task_1', 'city_id': 'city', 'user_prompt': 'Choose a cafe'})
    put(folder / 'task_provenance.json', {'world_id':'world_test','city_id':'city','generation_context': {'city_version': 'v2'}})
    put(folder / 'task_sandbox.json', {'time_context': {'episode_start': '2026-09-17T10:00:00+00:00'}, 'correct_answer': 'SECRET_GT'})
    put(folder / 'static_app_state.json', {'apps': {'LocalServiceApp': {'services': [{'name': 'Cafe', 'location': {'lat': 1, 'lng': 2}, 'open_status': 'open'}]}}})
    put(folder / 'env_events.json', {'schema_version': 'geoword.dynamic_events.v2', 'env_events': [{'event_id': 'e1', 'app': 'LocalServiceApp', 'activation_time': '2026-09-17T11:00:00+00:00', 'state_field': 'service.open_status', 'scope': {'type': 'service', 'id': 'private'}, 'public_scope': {'name': 'Cafe', 'location': {'lat': 1, 'lng': 2}}, 'state_patch': {'open_status': 'closed'}}]})
    monkeypatch.setattr(runner, '_resolve_place_world_snapshot', lambda *a: {'world_paths': [], 'local_service_world_state_paths': []})
    from app.runtime import asset_bindings
    monkeypatch.setattr(asset_bindings, '_resolve_route_cache', lambda *a: '')
    payload=json.loads((folder/'env_events.json').read_text())
    event=payload['env_events'][0]
    put(folder.parent.parent/'city_event_ledger.json',{'city_id':'city','events':[{**event,'world_binding':'world_test/city/v2'}]})
    return folder

def test_dynamic_preview_is_runtime_projection_and_read_only(tmp_path, monkeypatch):
    folder = fixture(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    ref = folder.relative_to(tmp_path).as_posix()
    first = preview(tmp_path, ref)
    assert not [a for a in first['apps'] if a.get('error')]
    later = preview(tmp_path, ref, 1)

    def service(data):
        return next((a for a in data['apps'] if a['name'] == 'LocalServiceApp'))['rows'][0]
    assert service(first)['content']['open_status'] == 'open'
    assert service(later)['content']['open_status'] == 'closed'
    assert service(later)['changed']
    assert service(preview(tmp_path, ref))['content']['open_status'] == 'open'
    assert 'SECRET_GT' not in json.dumps(later)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert catalog(tmp_path)['tasks'][0]['version'] == '2'

def test_static_does_not_apply_event_file(tmp_path, monkeypatch):
    folder = fixture(tmp_path, monkeypatch, False)
    result = preview(tmp_path, str(folder.relative_to(tmp_path)))
    assert len(result['nodes']) == 1
    assert next((a for a in result['apps'] if a['name'] == 'LocalServiceApp'))['rows'][0]['content']['open_status'] == 'open'
    with pytest.raises(ValueError):
        preview(tmp_path, str(folder.relative_to(tmp_path)), 1)

def test_invalid_path_and_node_rejected(tmp_path, monkeypatch):
    folder = fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        resolve_folder(tmp_path, '../outside')
    with pytest.raises(ValueError):
        preview(tmp_path, str(folder.relative_to(tmp_path)), -1)

def test_asset_errors_are_not_empty_data(tmp_path, monkeypatch):
    import app.runtime.episode as runner
    folder = fixture(tmp_path, monkeypatch, False)

    def fail(*a):
        raise ValueError('hash mismatch')
    monkeypatch.setattr(runner, '_resolve_place_world_snapshot', fail)
    with pytest.raises(ValueError,match='hash mismatch'):
        preview(tmp_path, str(folder.relative_to(tmp_path)))
