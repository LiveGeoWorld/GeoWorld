import hashlib
import json
from vis.tasks.app_events import static_events

def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf8')

def batch(root, name, date, title='Notice', copies=2, valid=True):
    base = root / 'safety_planning/world_test/city' / name
    put(base / 'batch_metadata.json', {'created_at': date})
    view = dict(schema_version='safety_public_event_view.v1', world_binding='binding', apps={'WeatherApp': [dict(title=title, description='Rain')], 'CityNewsApp': []})
    view['content_hash'] = hashlib.sha256(json.dumps(view, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    for i in range(copies):
        folder = base / 'tasks' / str(i)
        put(folder / 'task.json', {'task_type': 'safety_planning', 'task_id': str(i)})
        put(folder / 'task_sandbox.json', {'gold': 'SECRET', 'safety_event_view': {'world_binding': 'binding', 'path': 'safety_event_view.json'}} if valid else {})
        if valid:
            put(folder / 'safety_event_view.json', view)
    return base

def read(root):
    return static_events(root, 'world_test', [{'city_id': 'city', 'city_name': 'City'}])

def test_latest_by_creation_time_and_shared_view_once(tmp_path):
    batch(tmp_path, 'zzz_old', '2026-09-01', 'Old')
    batch(tmp_path, 'aaa_new', '2026-09-17', 'New')
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = read(tmp_path)
    assert [r['content']['title'] for r in result['apps']['WeatherApp']] == ['New']
    assert result['sources'][0]['batch'] == 'aaa_new'
    assert 'SECRET' not in json.dumps(result)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}

def test_missing_latest_view_does_not_fallback(tmp_path):
    batch(tmp_path, 'old', '2026-09-01')
    batch(tmp_path, 'new', '2026-09-17', valid=False)
    result = read(tmp_path)
    assert result['apps']['WeatherApp'] == []
    assert result['sources'][0]['status'] == 'error'

def test_no_batch_is_not_read_failure(tmp_path):
    assert read(tmp_path)['sources'][0]['status'] == 'no_batch'

def test_pending_sync_never_served(tmp_path):
    base = batch(tmp_path, 'new', '2026-09-17')
    put(base / 'safety_view_sync.json', {'status': 'pending'})
    assert read(tmp_path)['sources'][0]['status'] == 'error'

def test_corrupt_duplicate_is_not_ignored(tmp_path):
    base = batch(tmp_path, 'new', '2026-09-17')
    view = json.loads((base / 'tasks/1/safety_event_view.json').read_text())
    view['apps']['WeatherApp'][0]['title'] = 'Changed'
    put(base / 'tasks/1/safety_event_view.json', view)
    result = read(tmp_path)
    assert result['sources'][0]['status'] == 'error'
    assert result['apps']['WeatherApp'] == []
