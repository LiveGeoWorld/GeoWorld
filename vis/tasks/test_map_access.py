import json
import pytest
from vis.tasks.map_access import invoke
from app.apps.map_app import MapAppSandbox

def assets(tmp_path):
    world = tmp_path / 'world.json'
    world.write_text(json.dumps({'places': [{'place_id': 'private_a', 'name': 'Cafe A', 'address': 'One Street', 'primary_type': 'cafe', 'location': {'lat': 1, 'lng': 2}}, {'place_id': 'private_b', 'name': 'Cafe B', 'address': 'Two Street', 'primary_type': 'cafe', 'location': {'lat': 3, 'lng': 4}}]}), encoding='utf8')
    routes = tmp_path / 'routes.json'
    routes.write_text(json.dumps({'cache': [{'route_id': 'hidden_route', 'origin_place_id': 'private_a', 'dest_place_id': 'private_b', 'mode': 'WALK', 'route': {'distance_m': 200, 'duration_s': 160}}]}), encoding='utf8')
    return (world, routes)

def test_public_methods_equal_real_sandbox_and_no_writes(tmp_path):
    (world, routes) = assets(tmp_path)
    app = MapAppSandbox(str(routes), [str(world)])
    before = {p: p.read_bytes() for p in (world, routes)}
    a = {'name': 'Cafe A', 'location': {'lat': 1, 'lng': 2}}
    b = {'name': 'Cafe B', 'location': {'lat': 3, 'lng': 4}}
    for (tool, args) in [('search_places', {'query': 'Cafe', 'limit': 2}), ('get_place_detail', a), ('query_routes', {'origin': a, 'destination': b, 'mode': 'WALK'})]:
        result = invoke([world], [routes], tool, args)
        assert result['observation'] == getattr(app, tool)(**args)
        assert result['tool'] == 'MapApp__' + tool
        assert 'private_' not in json.dumps(result)
        assert 'hidden_route' not in json.dumps(result)
    assert before == {p: p.read_bytes() for p in before}

def test_public_address_search_and_no_fake_route(tmp_path):
    (world, routes) = assets(tmp_path)
    assert invoke([world], [routes], 'search_places', {'query': 'One Street'})['observation']['count'] == 1
    result = invoke([world], [], 'query_routes', {'origin': {}, 'destination': {}})['observation']
    assert result['count'] == 0 and result['routes'] == []
    assert result['status'] == 'place_not_resolved'

def test_world_switch_and_cache_refresh(tmp_path):
    (world, routes) = assets(tmp_path)
    assert invoke([world], [routes], 'search_places', {'query': 'Cafe'})['observation']['count'] == 2
    world.write_text(json.dumps({'places': []}), encoding='utf8')
    assert invoke([world], [], 'search_places', {'query': 'Cafe'})['observation']['count'] == 0

def test_no_write_tools_or_unknown_methods(tmp_path):
    (world, routes) = assets(tmp_path)
    with pytest.raises(ValueError):
        invoke([world], [routes], '_load_world', {})
    with pytest.raises(ValueError):
        invoke([world], [routes], 'make_reservation', {})

def test_public_browse_pagination_and_optional_geometry(tmp_path):
    (world, routes) = assets(tmp_path)
    raw = json.loads(routes.read_text())
    raw['cache'][0]['route']['steps'] = [{'polyline': {'encodedPolyline': '_p~iF~ps|U_ulLnnqC_mqNvxq`@'}}]
    raw['cache'][0]['route']['encoded_polyline'] = '_p~iF~ps|U_ulLnnqC_mqNvxq`@'
    routes.write_text(json.dumps(raw))
    page = invoke([world], [routes], 'search_places', {'query': '', 'limit': 1, 'offset': 1})['observation']
    assert page['total'] == 2 and page['results'][0]['name'] == 'Cafe B'
    args = {'origin': {'name': 'Cafe A', 'location': {'lat': 1, 'lng': 2}}, 'destination': {'name': 'Cafe B', 'location': {'lat': 3, 'lng': 4}}}
    plain = invoke([world], [routes], 'query_routes', args)['observation']
    assert 'encoded_polyline' not in plain['routes'][0]
    result = invoke([world], [routes], 'query_routes', {**args, 'detail_level': 'geometry'})['observation']
    assert result['routes'][0]['encoded_polyline'] == '_p~iF~ps|U_ulLnnqC_mqNvxq`@'
    assert 'hidden_route' not in json.dumps(result)

def test_route_statuses_do_not_infer_no_route_from_zero(tmp_path):
    (world, routes) = assets(tmp_path)
    raw = json.loads(routes.read_text())
    base = raw['cache'][0]
    raw['cache'] = [{**base, 'mode': mode, 'route': route} for (mode, route) in [('WALK', {'distance_m': 200, 'duration_s': 160}), ('TRANSIT', {'available': False, 'summary': 'UNAVAILABLE', 'distance_m': 0, 'duration_s': 0}), ('DRIVE', {'distance_m': 0, 'duration_s': 0})]]
    routes.write_text(json.dumps(raw))
    args = {'origin': {'name': 'Cafe A', 'location': {'lat': 1, 'lng': 2}}, 'destination': {'name': 'Cafe B', 'location': {'lat': 3, 'lng': 4}}, 'detail_level': 'geometry'}
    result = invoke([world], [routes], 'query_routes', args)['observation']
    assert result['routes'][0]['distance_m'] == 200
    assert result['routes'][1]['available'] is False
    assert result['routes'][1]['summary'] == 'UNAVAILABLE'
    assert 'available' not in result['routes'][2]
    raw['cache'] = raw['cache'][:1]
    routes.write_text(json.dumps(raw))
    result = invoke([world], [routes], 'query_routes', args)['observation']
    assert [r['mode'] for r in result['routes']] == ['WALK']

def test_viewer_osm_except_registered_shanghai_places(tmp_path):
    (world, routes) = assets(tmp_path)
    args = {'origin': {'name': 'Cafe A', 'location': {'lat': 1, 'lng': 2}}, 'destination': {'name': 'Cafe B', 'location': {'lat': 3, 'lng': 4}}}
    normal = invoke([world], [routes], 'query_routes', args)
    shanghai = invoke([world], [routes], 'query_routes', args, [world])
    assert normal['viewer_basemap'] == 'osm'
    assert shanghai['viewer_basemap'] == 'configured'
    assert normal['observation'] == shanghai['observation']
