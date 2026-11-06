from copy import deepcopy
import pytest

from app.weather import APP_WEATHER
from app.runtime.environment import Environment
from app.apps.weather_app import WeatherApp


def test_only_event_query_is_public_and_no_default_weather():
    app = WeatherApp()
    assert [s['name'] for s in app.get_tool_schemas()] == ['get_alerts']
    assert [f.name for f in APP_WEATHER.functions] == ['get_alerts']
    assert app.get_alerts() == {'alerts': [], 'count': 0, 'updates': []}
    app._static_state = {'forecast': [{'temperature': 24}], 'current': {'condition': '晴'}}
    app._sandbox = {'risk_conditions': {'weather': 'rain', 'area': 'closure', 'transit': 'delay'}}
    assert app.get_alerts()['alerts'] == []
    assert app.get_forecast()['forecast'] == []
    assert app.get_current_conditions() == {'status': 'unsupported_operation'}


def test_weather_event_publication_scope_expiry_and_verbatim_content():
    app = WeatherApp()
    env = Environment()
    env.register_app(app)
    event = {'event_id': 'private_event', 'app': 'WeatherApp',
             'scope': {'type': 'district', 'id': 'private_district'},
             'public_scope': {'type': 'district', 'name': 'Central'},
             'activation_time': '2026-07-17T11:00:00+00:00',
             'end_time': '2026-07-17T12:00:00+00:00',
             'state_patch': {'condition': 'rain'},
             'public_message': 'Heavy rain in Central until noon.'}
    saved = deepcopy(event)
    env.load_dynamic_events([event])
    env.init_events('2026-07-17T10:00:00+00:00')
    assert app.get_alerts()['alerts'] == []
    assert env.time_manager.virtual_time_iso().startswith('2026-07-17T10:00')
    env.advance_virtual_time('2026-07-17T11:00:00+00:00')
    result = app.get_alerts('Central')
    assert result['count'] == 1
    assert result['alerts'][0]['description'] == event['public_message']
    assert result['alerts'][0]['end_time'] == event['end_time']
    assert 'private_' not in str(result)
    assert app.get_alerts('Other')['alerts'] == []
    env.advance_virtual_time('2026-07-17T12:00:00+00:00')
    assert app.get_alerts()['alerts'] == []
    assert event == saved


def test_saved_safety_notice_remains_verbatim_without_other_risk_synthesis():
    app = WeatherApp()
    notice = {'scope': {'name': 'Central'}, 'message': 'Stored storm notice.',
              'active_window': {'start': '2026-07-17T10:00:00+00:00',
                                'end': '2026-07-17T12:00:00+00:00'}}
    app._safety_view = [deepcopy(notice)]
    assert app.get_alerts('Central')['alerts'] == [notice]
    assert app.get_alerts('Other')['alerts'] == []
    assert app._safety_view == [notice]


@pytest.mark.parametrize('timing', [
    {'active_window': {'start': '2026-07-17T10:00:00+00:00', 'end': '2026-07-17T12:00:00+00:00'}},
    {'activation_time': '2026-07-17T10:00:00+00:00', 'end_time': '2026-07-17T12:00:00+00:00'},
    {'timestep': '2026-07-17T10:00:00', 'end_time': '2026-07-17T12:00:00'},
])
def test_saved_baseline_weather_respects_activation_and_half_open_expiry(timing):
    app = WeatherApp()
    notice = {'scope': {'name': 'Central'}, 'description': 'Stored rain notice.', **timing}
    app._static_state = {'alerts': [deepcopy(notice)]}
    for instant, expected in [('09:00', []), ('10:00', [notice]), ('11:59', [notice]), ('12:00', [])]:
        app.refresh_dynamic_state(f'2026-07-17T{instant}:00+00:00')
        assert app.get_alerts('Central')['alerts'] == expected
    assert app._static_state['alerts'] == [notice]


def test_future_published_baseline_weather_is_hidden():
    app = WeatherApp()
    app._static_state = {'alerts': [{'scope': {'name': 'Central'}, 'description': 'Stored rain notice.',
        'active_window': {'start': '2026-07-17T10:00:00+00:00', 'end': '2026-07-17T12:00:00+00:00'},
        'published_at': '2026-07-17T11:00:00+00:00'}]}
    app.refresh_dynamic_state('2026-07-17T10:30:00+00:00')
    assert app.get_alerts()['alerts'] == []


def test_legacy_baseline_scope_keeps_public_name_without_exposing_private_id():
    app = WeatherApp()
    saved = {'scope': {'type': 'district', 'id': 'private_district', 'name': 'Central'},
             'description': 'Stored rain notice.'}
    app._static_state = {'alerts': [deepcopy(saved)]}
    for query in ['Central', 'private_district', '']:
        result = app.get_alerts(query)
        assert result['count'] == 1
        assert result['alerts'][0]['scope'] == {'type': 'district', 'name': 'Central'}
        assert 'private_district' not in str(result)
    assert app._static_state['alerts'] == [saved]
