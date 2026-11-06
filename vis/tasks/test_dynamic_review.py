import hashlib
import json
from copy import deepcopy
from vis.tasks.dynamic_review import load_dynamic_review, patch_event
from vis.tasks.service_review import project_status
from vis.tasks.test_app_events import put
CITY = {'city_id': 'city', 'active_version': 2}
IDENTITY = {'name': 'Cafe', 'location': {'lat': 1, 'lng': 2}, 'place_id': 'p1'}

def fixture(root):
    source = root / 'itinerary/world/city/old/tasks/task1'
    put(source / 'task.json', {'task_id': 'original'})
    put(source / 'task_sandbox.json', {})
    put(source / 'task_provenance.json', {'world_id': 'world', 'generation_context': {'city_version': 'v2'}})
    folder = root / 'dynamic_replanning/world/city/new/tasks/dynamic1'
    put(folder.parent.parent / 'batch_metadata.json', {'created_at': '2026-09-17'})
    put(folder / 'task.json', {'task_id': 'dynamic1'})
    put(folder / 'task_sandbox.json', {'source_task': {'task_type': 'itinerary', 'task_id': 'original', 'task_folder': str(source), 'task_json_sha256': hashlib.sha256((source / 'task.json').read_bytes()).hexdigest(), 'sandbox_sha256': hashlib.sha256((source / 'task_sandbox.json').read_bytes()).hexdigest()}, 'time_context': {'episode_start': '2026-09-17T09:00:00+08:00'}})
    sandbox = json.loads((folder / 'task_sandbox.json').read_text())
    sandbox['source_task']['task_folder'] = 'C:/old/outputs/benchmarks/itinerary/world/city/old/tasks/task1'
    put(folder / 'task_sandbox.json', sandbox)
    put(folder / 'static_app_state.json', {'apps': {'LocalServiceApp': {'services': [IDENTITY]}}})
    close = {'event_id': 'close', 'app': 'LocalServiceApp', 'scope': {'type': 'service', 'id': 'p1'}, 'public_scope': {'type': 'service'}, 'state_field': 'service.open_status', 'activation_time': '2026-09-17T10:00:00+08:00', 'state_patch': {'open_status': 'closed', 'availability': 'unavailable'}}
    recovery = {**deepcopy(close), 'event_id': 'recover', 'activation_time': '2026-09-17T11:00:00+08:00', 'causal_parents': ['close'], 'state_patch': {'open_status': 'open', 'availability': 'available'}}
    put(folder / 'env_events.json', {'schema_version': 'geoword.dynamic_events.v2', 'env_events': [recovery, close]})
    return (folder, source)

def test_real_shape_legacy_identity_and_recovery(tmp_path):
    (folder, source) = fixture(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*.json')}
    data = load_dynamic_review(tmp_path, 'world', [CITY])['city']
    assert data['error'] is None and len(data['events']) == 2
    for (time, expected) in [('09:59', '营业中'), ('10:00', '受事件影响，服务不可用'), ('11:00', '营业中')]:
        row = {**IDENTITY, 'regular_status': '营业中'}
        project_status(row, {}, data['events'], f'2026-09-17T{time}:00+08:00', data['error'])
        assert row['service_status'] == expected
    assert before == {p: p.read_bytes() for p in before}
    put(source / 'task.json', {'task_id': 'changed'})
    assert '哈希' in load_dynamic_review(tmp_path, 'world', [CITY])['city']['error']

def test_missing_identity_and_parent_reported(tmp_path):
    (folder, _) = fixture(tmp_path)
    payload = json.loads((folder / 'env_events.json').read_text())
    payload['env_events'][0]['causal_parents'] = ['absent']
    put(folder / 'env_events.json', payload)
    result = load_dynamic_review(tmp_path, 'world', [CITY])['city']
    assert result['error'] and (not result['events'])

def test_expiry_and_field_specific_updates():
    event = {'public_scope': IDENTITY, 'scope': {}, 'activation_time': '2026-09-17T10:00:00+08:00', 'end_time': '2026-09-17T11:00:00+08:00', 'state_patch': {'queue_time_min': 12}}
    events = [patch_event(event, [])]
    row = {**IDENTITY, 'regular_status': '营业中'}
    project_status(row, {}, events, '2026-09-17T10:30:00+08:00')
    assert row['service_status'] == '营业中'
    assert row['review_impacts'][0]['status'] == '排队约 12 分钟'
    project_status(row, {}, events, '2026-09-17T11:00:00+08:00')
    assert row['review_impacts'] == []
    untouched = {**IDENTITY}
    project_status(untouched, {}, events, '')
    assert untouched == IDENTITY

def test_initial_service_state_is_retained_and_other_apps_do_not_close_service(tmp_path):
    (folder, _) = fixture(tmp_path)
    put(folder / 'static_app_state.json', {'apps': {'LocalServiceApp': {'services': [{**IDENTITY, 'queue_time_min': 5}]}}})
    payload = json.loads((folder / 'env_events.json').read_text())
    payload['env_events'].append({'event_id': 'traffic', 'app': 'CityNewsApp', 'scope': {'type': 'route_operation', 'id': 'leg_1'}, 'activation_time': '2026-09-17T09:00:00+08:00', 'state_field': 'transit.operation_status', 'state_patch': {'operational_status': 'disrupted'}})
    put(folder / 'env_events.json', payload)
    data = load_dynamic_review(tmp_path, 'world', [CITY])['city']
    assert data['error'] is None
    row = {**IDENTITY, 'regular_status': '营业中'}
    project_status(row, {}, data['events'], '2026-09-17T09:30:00+08:00')
    assert row['service_status'] == '营业中'
    assert row['review_impacts'][0]['status'] == '排队约 5 分钟'

def test_safety_baseline_comes_from_bound_source_not_latest(tmp_path):
    (folder, old) = fixture(tmp_path)
    source = tmp_path / 'safety_planning/world/city/frozen/tasks/task1'
    for name in ('task.json', 'task_sandbox.json', 'task_provenance.json'):
        put(source / name, json.loads((old / name).read_text()))
    sandbox = json.loads((folder / 'task_sandbox.json').read_text())
    sandbox['source_task'].update(task_type='safety_planning', task_folder='outputs/benchmarks/safety_planning/world/city/frozen/tasks/task1')
    put(folder / 'task_sandbox.json', sandbox)
    put(folder / 'env_events.json', {'env_events': []})
    put(source / 'safety_evidence_history.json', {'world_binding': 'world:city:v2', 'entries': [{'event': {'events': [{'title': 'Bound initial event', 'scope_ref': 'area', 'active_window': {'start': '2026-09-17T09:00:00+08:00', 'end': '2026-09-17T10:00:00+08:00'}, 'effects': [{'kind': 'unavailable', 'where': []}]}]}, 'context': {'geographic_anchors': [{'scope_ref': 'area', 'geometry': {'type': 'Point', 'coordinates': [2, 1]}}]}}]})
    put(tmp_path / 'safety_planning/world/city/newest/batch_metadata.json', {'created_at': '2099-01-01'})
    data = load_dynamic_review(tmp_path, 'world', [CITY])['city']
    assert data['error'] is None
    row = {**IDENTITY, 'regular_status': '营业中'}
    project_status(row, {}, data['events'], '2026-09-17T09:30:00+08:00')
    assert row['review_impacts'][0]['title'] == 'Bound initial event'
    assert row['service_status'] == '受事件影响，服务不可用'
