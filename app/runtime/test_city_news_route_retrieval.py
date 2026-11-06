import json
import pytest
from app.apps.city_news import CityNewsApp
from app.runtime.environment import Environment


def test_cache_reference_uses_saved_route_geometry_without_changing_identity():
    from app.apps.map_app import MapAppSandbox
    from app.apps.city_news_route_retrieval import route_districts
    route = {'polyline_points': [{'lat': 0, 'lng': 0}, {'lat': 0, 'lng': 0.01}]}
    cache = {'cache': [
        {'route_id': 'geometry_owner', 'origin_place_id': 'other', 'route': route},
        {'route_id': 'journey', 'origin_place_id': 'p', 'route_ref': 'geometry_owner'},
    ]}
    app = MapAppSandbox(route_cache_data=cache)
    assert app._frozen_routes['journey']['origin_place_id'] == 'p'
    assert 'route' not in cache['cache'][1]
    assert route_districts(
        {'scope': {'type': 'route_operation', 'id': 'journey'}},
        app._frozen_routes, [{'place_id': 'p', 'city_id': 'c'}],
        [{'district_id': 'd', 'city_id': 'c',
          'geometry': {'kind': 'point', 'data': {'lat': 0, 'lng': 0}}}],
    ) == {'d'}


def test_participant_route_origin_uses_frozen_destination_city():
    from app.apps.city_news_route_retrieval import route_districts
    routes = {'r': {'origin_place_id': 'char_tokyo_001', 'dest_place_id': 'venue',
                    'route': {'polyline_points': [{'lat': 0, 'lng': 0},
                                                  {'lat': 0, 'lng': 0.01}]}}}
    places = [{'place_id': 'char_tokyo_001'},
              {'place_id': 'venue', '_city_id': 'tokyo'}]
    districts = [{'district_id': name, 'city_id': city,
                  'geometry': {'kind': 'point', 'data': {'lat': 0, 'lng': 0}}}
                 for name, city in [('local', 'tokyo'), ('foreign', 'london')]]
    assert route_districts({'scope': {'id': 'r'}}, routes, places, districts) == {'local'}
    places[1].pop('_city_id')
    with pytest.raises(ValueError, match='city_unresolved'):
        route_districts({'scope': {'id': 'r'}}, routes, places, districts)


@pytest.mark.parametrize('local', [True, False])
@pytest.mark.parametrize('runtime', [True, False])
@pytest.mark.parametrize('shared_normalization', [True, False])
def test_route_multi_district_local_vs_legacy_and_unchanged_payload(tmp_path, local, runtime, shared_normalization):
    districts = [{'district_id': key, 'city_id': city, 'name': key,
                  'geometry': {'kind': 'point', 'data': {'lat': lat, 'lng': lng}}}
                 for key, city, lat, lng in [('a','c',0.01,0), ('b','c',0.01,0.02),
                                             ('d','c',0,0.1), ('foreign','other',0,0)]]
    (tmp_path/'world_slice.json').write_text(json.dumps({'districts': districts,
        'places': [{'place_id':'p','city_id':'c','name':'Origin','location':{'lat':0,'lng':0}}]}), encoding='utf-8')
    route = {'route_id':'r','origin_place_id':'p', 'route':
             {'polyline_points':[{'lat':0,'lng':0},{'lat':0,'lng':0.1}]}}
    (tmp_path/'task_sandbox.json').write_text(json.dumps({'dynamic_route_graph':{'route_records':[route]}}), encoding='utf-8')
    app = CityNewsApp(str(tmp_path))
    event = {'scope':{'type':'route_operation','id':'r'},
             'public_scope':{'origin_name':'Li Meiqin','dest_name':'Taikang Dumplings', 'mode':'WALK'},
             'public_message':'Traffic notice'}
    if local:
        event['geometry']={'type':'LineString','coordinates':[[0,0],[0.02,0]]}
    if runtime:
        env = Environment()
        env.register_app(app)
        event.update(event_id='traffic', app='CityNewsApp',
                     activation_time='2026-07-17T09:00:00+00:00',
                     state_patch={'delay_minutes': 12})
        payload = [event]
        if shared_normalization:
            from app.runtime.dynamic_events import normalize_event_payload
            payload = normalize_event_payload(payload)
        env.load_dynamic_events(payload)
        env.init_events('2026-07-17T09:00:00+00:00')
        assert env.event_states['traffic'] == 'applied'
    else:
        app.inject_entry(event)
    assert len(app.get_info(district='a')) == 1
    assert len(app.get_info(district='b')) == 1
    assert len(app.get_info(district='d')) == (0 if local else 1)
    assert app.get_info(district='foreign') == []
    assert app.get_info(district='a') == app.get_info()
    public = app.get_info()[0]
    expected = event.get('geometry') or {'type':'LineString','coordinates':[[0,0],[0.1,0]]}
    assert json.loads(json.dumps(public['scope'])) == {
        'type':'route_segment', 'geometry':expected, 'mode':'WALK'}
    assert public['description'] == 'Traffic notice'
    assert 'Li Meiqin' not in json.dumps(public)
    assert 'Taikang Dumplings' not in json.dumps(public)
    assert 'origin_name' not in public['scope'] and 'dest_name' not in public['scope']
    assert app._event_history[0]['public_scope'] == event['public_scope']
    if runtime:
        history = env._dynamic_notice_histories['CityNewsApp'].get_notice_history()['notices']
        assert history[0]['scope'] == public['scope']
        assert 'Li Meiqin' not in json.dumps(history)


def test_runtime_unresolved_local_steps_do_not_fall_back_to_whole_route(tmp_path):
    (tmp_path/'world_slice.json').write_text(json.dumps({
        'districts': [{'district_id': 'd', 'city_id': 'c', 'name': 'District',
                      'geometry': {'kind': 'point', 'data': {'lat': 0, 'lng': 0}}}],
        'places': [{'place_id': 'p', 'city_id': 'c'}]}), encoding='utf-8')
    (tmp_path/'task_sandbox.json').write_text(json.dumps({
        'dynamic_route_graph': {'route_records': [{
            'route_id': 'r', 'origin_place_id': 'p',
            'route': {'polyline_points': [{'lat': 0, 'lng': 0}, {'lat': 0, 'lng': 0.1}]}}]}}), encoding='utf-8')
    env = Environment()
    env.register_app(CityNewsApp(str(tmp_path)))
    env.load_dynamic_events([{
        'event_id': 'traffic', 'app': 'CityNewsApp',
        'scope': {'type': 'route_operation', 'id': 'r'},
        'activation_time': '2026-07-17T09:00:00+00:00',
        'spatial_scope': {'kind': 'route_segment', 'step_indices': [0]},
        'public_message': 'Traffic notice', 'state_patch': {'delay_minutes': 12}}])
    with pytest.raises(ValueError, match='resolvable cached shape'):
        env.init_events('2026-07-17T09:00:00+00:00')


def test_route_nearest_fallback_and_missing_local_geometry_is_not_whole_route():
    from app.apps.city_news_route_retrieval import route_districts
    route={'route_id':'r','origin_place_id':'p','route':{'polyline_points':[{'lat':0,'lng':0},{'lat':0,'lng':0.01}]}}
    districts=[{'district_id':k,'city_id':'c','geometry':{'kind':'point','data':{'lat':lat,'lng':0}}}
               for k,lat in [('near',0.1),('far',1)]]
    entry={'scope':{'type':'route_operation','id':'r'}}
    places=[{'place_id':'p','city_id':'c'}]
    assert route_districts(entry,{'r':route},places,districts)=={'near'}
    entry['spatial_scope']={'kind':'route_segment','step_indices':[0]}
    with pytest.raises(ValueError, match='resolvable cached shape'):
        route_districts(entry,{'r':route},places,districts)
