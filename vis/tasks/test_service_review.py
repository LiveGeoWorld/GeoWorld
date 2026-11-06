import copy
import json
from shapely.geometry import Point
from vis.tasks.service_review import project_status, load_review_events
from vis.tasks.test_app_events import batch, put

def event(**extra):
    return {'title': 'Electrical shutdown', 'active_window': {'start': '2026-09-17T10:00:00+08:00', 'end': '2026-09-17T12:00:00+08:00'}, 'effects': [{'kind': 'unavailable', 'where': [], 'reason': 'Electrical fault'}], **extra}

def row():
    return {'location': {'lat': 1, 'lng': 2}, 'regular_status': '营业中'}

def test_time_space_and_no_time():
    events = [(event(), Point(2, 1).buffer(0.01))]
    for (time, status) in [('', None), ('2026-09-17T02:00Z', '受事件影响，服务不可用'), ('2026-09-17T04:00Z', '营业中'), ('2026-09-17T01:59Z', '营业中')]:
        item = row()
        project_status(item, {}, events, time)
        assert item.get('service_status') == status
    item = row()
    project_status(item, {}, [(event(), Point(4, 3).buffer(0.01))], '2026-09-17T03:00Z')
    assert item['service_status'] == '营业中'
    assert item['review_impacts'] == []

def test_predicates_delay_and_errors_do_not_invent_closure():
    item = row()
    e = event(effects=[{'kind': 'unavailable', 'where': [{'field': 'dine_in', 'equals': True}]}])
    events = [(e, Point(2, 1).buffer(0.01))]
    project_status(item, {}, events, '2026-09-17T03:00Z')
    assert item['service_status'] == '事件状态无法确认'
    project_status(item, {'map_projection': {'google_place_details': {'dine_in': False}}}, events, '2026-09-17T03:00Z')
    assert item['service_status'] == '营业中'
    e['effects'] = [{'kind': 'delay', 'where': [], 'delay_minutes': 10, 'transportation_mode': 'WALK'}]
    project_status(item, {}, events, '2026-09-17T03:00Z')
    assert item['service_status'] == '营业中'
    assert item['review_impacts'][0]['status'] == '通行延误'
    assert 'queue_time_min' not in json.dumps(item)
    project_status(item, {}, [], '2026-09-17T03:00Z', '缺少事件记录')
    assert item['service_status'] == '事件状态无法确认'

def test_missing_private_history_is_not_normal_and_no_fallback(tmp_path):
    batch(tmp_path, 'new', '2026-09-17')
    result = load_review_events(tmp_path, 'world_test', [{'city_id': 'city', 'active_version': 1}])
    assert result['city']['error']
    assert not result['city']['events']

def test_review_never_mutates_event_or_public_projection():
    from app.assets.safety.event_contract import public_event
    e = event(event_kind='infrastructure', app='CityNewsApp', description='Power failure')
    anchor = {'public_scope': {'name': 'Cafe'}}
    before = copy.deepcopy(e)
    public_before = public_event(e, anchor)
    project_status(row(), {}, [(e, Point(2, 1).buffer(0.01))], '2026-09-17T03:00Z')
    assert e == before and public_event(e, anchor) == public_before
    assert 'effects' not in public_before

def test_bound_history_consistency_and_version(monkeypatch, tmp_path):
    from vis.tasks import service_review
    from app.assets.safety.event_contract import public_event
    base = batch(tmp_path, 'new', '2026-09-17', copies=1)
    e = event(event_kind='infrastructure', app='CityNewsApp', description='Fault', scope_ref='scope')
    anchor = {'scope_ref': 'scope', 'public_scope': {'name': 'Cafe'}, 'geometry': {'type': 'Point', 'coordinates': [2, 1]}}
    history = {'world_binding': 'world_test:city:v1', 'entries': [{'owner': '0', 'event': {'events': [e]}, 'context': {'geographic_anchors': [anchor]}}]}
    put(base / 'tasks/0/safety_evidence_history.json', history)
    public = {'sources': [{'status': 'ready', 'batch': 'new', 'city_name': 'City'}], 'apps': {'CityNewsApp': [{'content': public_event(e, anchor), 'city': 'City', 'batch': 'new'}]}}
    monkeypatch.setattr(service_review, 'static_events', lambda *a: public)

    def load(version=1):
        return load_review_events(tmp_path, 'world_test', [{'city_id': 'city', 'active_version': version}])['city']
    assert len(load()['events']) == 1 and load()['error'] is None
    assert load(2)['error']
    history['entries'][0]['event']['events'][0]['title'] = 'Changed'
    put(base / 'tasks/0/safety_evidence_history.json', history)
    assert load()['error']
