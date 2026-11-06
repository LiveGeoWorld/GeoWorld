import json
import pytest
from vis.tasks.app_access import regular_service_status
from vis.tasks.app_access import invoke
from app.apps.local_service import LocalServiceApp
from app.apps.city_news import CityNewsApp
from app.apps.weather_app import WeatherApp

def test_service_same_observation_and_no_write(tmp_path):
    path = tmp_path / 'services.json'
    path.write_text(json.dumps({'services': [{'name': 'Cafe', 'location': {'lat': 1, 'lng': 2}, 'open_status': 'open'}]}))
    before = path.read_bytes()
    args = {'name': 'Cafe', 'location': {'lat': 1, 'lng': 2}}
    for tool in ('get_service_detail', 'check_availability'):
        result = invoke(tmp_path, 'world', [], [path], 'LocalServiceApp', tool, args, 'static')
        assert result['observation'] == getattr(LocalServiceApp(world_state_paths=[path]), tool)(**args)
    assert path.read_bytes() == before

def test_regular_hours_local_timezone_and_boundaries():
    hours = {'periods': [{'open': {'day': 1, 'hour': 10}, 'close': {'day': 1, 'hour': 21}}]}
    assert regular_service_status(hours, '2026-09-14T09:00:00Z', 'Europe/London') == '营业中'
    assert regular_service_status(hours, '2026-09-14T20:00:00Z', 'Europe/London') == '非营业时间'
    assert regular_service_status(hours, '', 'Europe/London') is None
    with pytest.raises(ValueError):
        regular_service_status(hours, '2026-09-14T10:00', None)
    assert regular_service_status(None, '2026-09-14T10:00Z', 'Europe/London') == '营业时间未提供或无法计算'

def test_regular_hours_overnight_and_always_open():
    hours = {'periods': [{'open': {'day': 6, 'hour': 22}, 'close': {'day': 0, 'hour': 2}}]}
    assert regular_service_status(hours, '2026-09-20T01:00:00+08:00', 'Asia/Singapore') == '营业中'
    assert regular_service_status(hours, '2026-09-20T02:00:00+08:00', 'Asia/Singapore') == '非营业时间'
    assert regular_service_status({'periods': [{'open': {'day': 0, 'hour': 0}}]}, '2026-09-20T04:00Z', 'Asia/Singapore') == '营业中'

def test_weather_news_use_real_filters(monkeypatch, tmp_path):
    from vis.tasks import app_access
    notice = {'title': 'Rain', 'info_type': 'notice', 'scope': {'city_name': 'London'}, 'active_window': {'start': '2026-09-17T10:00:00+00:00', 'end': '2026-09-17T12:00:00+00:00'}}
    monkeypatch.setattr(app_access, 'static_events', lambda *a: {'sources': [], 'apps': {name: [{'content': notice}] for name in ('WeatherApp', 'CityNewsApp')}})
    for (cls, tool, args) in [(CityNewsApp, 'get_info', {'district_id': 'London', 'time_range': '2026-09-17T11:00:00+00:00'}), (CityNewsApp, 'get_info', {'district_id': 'Other'}), (WeatherApp, 'get_alerts', {'district_id': 'London'})]:
        app = cls()
        app._safety_view = [notice]
        assert invoke(tmp_path, 'world', [], [], app.name, tool, args, 'static')['observation'] == getattr(app, tool)(**args)

def test_no_writes_or_invented_dynamic_context(tmp_path):
    with pytest.raises(ValueError):
        invoke(tmp_path, 'world', [], [], 'LocalServiceApp', 'make_reservation', {}, 'static')
    with pytest.raises(ValueError):
        invoke(tmp_path, 'world', [], [], 'WeatherApp', 'get_alerts', {}, 'dynamic')
    with pytest.raises(ValueError):
        invoke(tmp_path, 'world', [], [], 'MessagingApp', 'search_contacts', {}, 'static')

def test_service_browse_is_paginated_public_projection(tmp_path):
    path = tmp_path / 'services.json'
    path.write_text(json.dumps({'services': [{'name': name, 'location': {'lat': i, 'lng': 2}, 'place_id': 'secret', 'private_field': 'not public'} for (i, name) in enumerate(['Cafe A', 'Cafe B', 'Shop C'])]}))
    result = invoke(tmp_path, 'world', [], [path], 'LocalServiceApp', 'browse_services', {'query': 'cafe', 'limit': 1, 'offset': 1}, 'static')
    assert result['observation']['total'] == 2
    assert result['observation']['results'][0]['name'] == 'Cafe B'
    assert 'secret' not in json.dumps(result) and 'private_field' not in json.dumps(result)
    assert result['read_method'] == 'LocalServiceApp__get_service_detail'
    dynamic = invoke(tmp_path, 'world', [], [path], 'LocalServiceApp', 'browse_services', {}, 'dynamic')
    assert dynamic['observation']['total'] == 3
    assert dynamic['event_state'] == 'review_projection'

def test_dynamic_service_keeps_base_but_not_frozen_operating_state(tmp_path):
    path = tmp_path / 'services.json'
    path.write_text(json.dumps({'services': [{'name': 'Cafe', 'location': {'lat': 1, 'lng': 2}, 'open_status': 'closed', 'availability_slots': [{'date': '2026-09-17'}]}]}))
    before = path.read_bytes()
    static = invoke(tmp_path, 'world', [], [path], 'LocalServiceApp', 'browse_services', {}, 'static')['observation']['results'][0]
    dynamic = invoke(tmp_path, 'world', [], [path], 'LocalServiceApp', 'browse_services', {}, 'dynamic')['observation']['results'][0]
    assert static['state_records']
    assert 'state_records' not in dynamic and 'slots' not in dynamic
    assert dynamic == {k: v for (k, v) in static.items() if k not in ('state_records', 'slots')}
    assert path.read_bytes() == before
    with pytest.raises(ValueError):
        invoke(tmp_path, 'world', [], [path], 'LocalServiceApp', 'browse_services', {}, 'invalid')

def test_review_hours_from_world_without_defaults_or_asset_writes(tmp_path):
    service = tmp_path / 'service.json'
    world = tmp_path / 'world.json'
    hours = {'periods': [{'open': {'day': 1, 'hour': 10}, 'close': {'day': 1, 'hour': 21}}]}
    service.write_text(json.dumps({'services': [{'name': 'Cafe', 'location': {'lat': 1, 'lng': 2}}]}))
    world.write_text(json.dumps({'places': [{'name': 'Cafe', 'location': {'lat': 1, 'lng': 2}, 'map_projection': {'google_place_details': {'regular_opening_hours': hours}}}]}))
    before = [p.read_bytes() for p in (service, world)]
    result = invoke(tmp_path, 'world', [], [service], 'LocalServiceApp', 'browse_services', {}, 'static', [world])['observation']['results'][0]
    assert result['opening_hours'] == hours
    assert 'duration_min' not in result and 'price_level' not in result
    assert before == [p.read_bytes() for p in (service, world)]
    assert LocalServiceApp(world_state_paths=[service]).get_service_detail('Cafe', {'lat': 1, 'lng': 2})['opening_hours'] == ''

def test_weather_review_filters_explicit_time(monkeypatch, tmp_path):
    from vis.tasks import app_access
    notice = {'title': 'Rain', 'scope': {'city_name': 'London'}, 'active_window': {'start': '2026-09-17T10:00:00+01:00', 'end': '2026-09-17T12:00:00+01:00'}}
    monkeypatch.setattr(app_access, 'static_events', lambda *a: {'sources': [], 'apps': {'WeatherApp': [{'content': notice}]}})

    def query(time):
        return invoke(tmp_path, 'world', [], [], 'WeatherApp', 'browse_weather', {'time': time}, 'static')['observation']
    assert query('') == [notice]
    assert query('2026-09-17T10:00:00+01:00') == [notice]
    assert query('2026-09-17T11:00:00+00:00') == []
    with pytest.raises(ValueError):
        query('2026-09-17T10:00')

def test_service_review_prices_rating_and_temporal_state(tmp_path):
    service = tmp_path / 'service.json'
    world = tmp_path / 'world.json'
    identity = {'name': 'Cafe', 'location': {'lat': 1, 'lng': 2}}
    service.write_text(json.dumps({'services': [{**identity, 'price_pp': 25, 'currency': 'SGD', 'open_status': 'closed'}]}))
    world.write_text(json.dumps({'places': [{**identity, 'map_projection': {'rating': 4.5, 'user_rating_count': 123, 'google_place_details': {'price_level': 'PRICE_LEVEL_MODERATE', 'takeout': True}}}]}))
    row = invoke(tmp_path, 'world', [], [service], 'LocalServiceApp', 'browse_services', {}, 'static', [world])['observation']['results'][0]
    assert row['rating'] == 4.5 and row['user_rating_count'] == 123
    assert row['price_level'] == 'PRICE_LEVEL_MODERATE'
    assert row['price_per_person'] == {'amount': 25, 'currency': 'SGD'}
    assert row['takeout'] is True
    assert 'open_status' not in row
    assert row['state_records'] == [{'values': {'open_status': 'closed'}, 'active_window': None}]
