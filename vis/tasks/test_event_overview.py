from shapely.geometry import Point
import pytest
from vis.tasks import event_overview

def test_static_map_is_whitelisted_and_time_filtered(monkeypatch):
    event = {'title': 'Notice', 'description': 'Closed', 'app': 'CityNewsApp', 'active_window': {'start': '2026-09-17T10:00:00+08:00', 'end': '2026-09-17T11:00:00+08:00'}, 'gt_protection': 'SECRET', 'effects': [{'kind': 'unavailable'}]}
    monkeypatch.setattr(event_overview, 'load_review_events', lambda *a: {'city': {'events': [(event, Point(2, 1))], 'error': None}})
    cities = [{'city_id': 'city', 'city_name': 'City', 'active_version': 2}]
    data = event_overview.overview('.', 'world', cities, 'city')
    assert len(data['events']) == 1
    assert 'SECRET' not in str(data) and 'effects' not in data['events'][0]
    assert data['events'][0]['geometry']['type'] == 'Point'
    filtered = event_overview.overview('.', 'world', cities, 'city', time='2026-09-17T11:00:00+08:00')
    assert not filtered['events']
    assert filtered['available_windows'] == [event['active_window']]
    with pytest.raises(ValueError):
        event_overview.overview('.', 'world', cities, 'foreign')
    with pytest.raises(ValueError):
        event_overview.overview('.', 'world', cities, 'city', time='2026-09-17T10:30')

def test_dynamic_keeps_unmapped_records_without_fake_geometry(monkeypatch):
    row = {'app': 'MessagingApp', 'title': 'Message', 'description': 'Update', 'active_window': {'start': '2026-09-17T10:00:00+08:00', 'end': None}, 'geometry': None}
    monkeypatch.setattr(event_overview, 'load_dynamic_review', lambda *a: {'city': {'events': [], 'error': None, 'overview': [row, row]}})
    data = event_overview.overview('.', 'world', [{'city_id': 'city', 'active_version': 1}], 'city', 'dynamic')
    assert data['events'] == [row]
