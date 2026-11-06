"""Integration of existing App methods through the standalone HTTP adapter."""
import json
from pathlib import Path
import pytest
from vis.tasks import app_http, server


def _registered_worlds():
    return [{'world_id': 'test_world', 'cities': [
        {'city_id': 'test_city', 'city_name': 'Test City', 'active_version': 1}]}]

class Handler:
    active_world_id = 'test_world'
    map_scope = {'scope': 'world', 'world_id': 'test_world'}

@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(app_http, 'PIPELINE_ROOT', tmp_path)
    def put(name, data):
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(data),encoding='utf8');return path
    places=[{'place_id':'private_a','name':'A','location':{'lat':1,'lng':2}},
            {'place_id':'private_b','name':'B','location':{'lat':3,'lng':4}}]
    put('map.json',{'places':places})
    put('routes.json',{'cache':[{'route_id':'private_route','origin_place_id':'private_a','dest_place_id':'private_b','mode':'WALK','route':{'duration_s':120,'distance_m':100}}]})
    put('outputs/worlds/test_world/cities/test_city.json',{'city_versions':[{'version':1,'artifacts':{'map_world':{'path':'map.json'},'route_cache':{'path':'routes.json'}}}]})
    output=[]
    monkeypatch.setattr(app_http,'send',lambda h,payload,status=200:output.append((status,payload)))
    return app_http.AppHTTP(Handler(),tmp_path/'outputs/benchmarks'),output,tmp_path


def test_map_search_detail_and_route_use_same_public_runtime(setup):
    api,out,root=setup
    before={p:p.read_bytes() for p in root.rglob('*.json')}
    for tool,args in [('search_places',{}),('get_place_detail',{'name':'A','location':{'lat':1,'lng':2}}),('query_routes',{'origin':{'name':'A','location':{'lat':1,'lng':2}},'destination':{'name':'B','location':{'lat':3,'lng':4}},'mode':'WALK'})]:
        assert api.handle('/api/app-content/map-query',{'world':['test_world'],'tool':[tool],'args':[json.dumps(args)]})
        status,result=out[-1];assert status==200
        assert 'private_' not in json.dumps(result)
    assert out[-1][1]['observation']['routes'][0]['duration_s']==120
    assert before=={p:p.read_bytes() for p in before}


def test_world_mismatch_and_missing_service_assets_are_explicit(setup):
    api,out,_=setup
    api.handle('/api/app-content/map-query',{'world':['other']})
    assert out[-1][0]==400 and 'changed' in out[-1][1]['error']
    api.handle('/api/app-content/query',{'world':['test_world'],'app':['LocalServiceApp']})
    assert out[-1][0]==400 and 'service state' in out[-1][1]['error']


def test_catalog_static_event_overview_and_invalid_preview(setup):
    api,out,_=setup
    for name in ['catalog','static-events','event-overview']:
        api.handle('/api/app-content/'+name,{'world':['test_world']})
        assert out[-1][0]==200
    api.handle('/api/app-content/preview',{'folder':['../outside']})
    assert out[-1][0]==400


def test_scope_bridge_uses_studio_scope(monkeypatch):
    h=Handler();h.path='/world-api/map-scope';out=[]
    monkeypatch.setattr(server,'send',lambda handler,data,status=200:out.append(data))
    assert server.serve_tasks(h)
    assert out==[h.map_scope]
