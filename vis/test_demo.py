import json,shutil,hashlib
from pathlib import Path
import pytest
from vis.demo import prepare_demo, ROOT
from app.apps.map_app import MapAppSandbox
from app.apps.local_service import LocalServiceApp


def example(tmp_path):
    shutil.copytree(ROOT/'examples/world_studio/registered_world',tmp_path/'examples/world_studio/registered_world')
    (tmp_path/'data/manifests').mkdir(parents=True)
    shutil.copy2(ROOT/'data/manifests/app_demo.json',tmp_path/'data/manifests/app_demo.json')
    return tmp_path


def test_example_installs_once_without_replacing_user_changes(tmp_path):
    root=example(tmp_path);path=prepare_demo(root)
    assert path.is_file()
    marker=path.parent/'keep.txt';marker.write_text('user data')
    path.write_text(path.read_text()+'\n')
    before=path.read_bytes();assert prepare_demo(root)==path
    assert marker.read_text()=='user data' and path.read_bytes()==before


def test_corrupt_bundle_is_rejected_before_registration(tmp_path):
    root=example(tmp_path)
    (root/'examples/world_studio/registered_world/artifacts/routes/london_smoke/v1/route_cache.json').write_text('{}')
    with pytest.raises(ValueError,match='integrity'):prepare_demo(root)
    assert not (root/'outputs/worlds/world_demo').exists()


def test_example_public_routes_and_service_records_match_frozen_assets(tmp_path):
    root=example(tmp_path);prepare_demo(root);assets=root/'outputs/worlds/world_demo/assets'
    before={p:p.read_bytes() for p in assets.glob('*.json')}
    world=json.loads((assets/'world.json').read_text(encoding='utf8'))
    app=MapAppSandbox(str(assets.parent/'artifacts/routes/london_smoke/v1/route_cache.json'),[str(assets/'world.json')])
    result=app.search_places(query='');assert result['count']==3
    points=[{'name':p['name'],'location':p['location']} for p in world['places']]
    routes=app.query_routes(points[0],points[1],mode='WALK',detail_level='geometry')
    assert routes['routes'][0]['duration_s']==242 and routes['routes'][0]['distance_m']==283
    services=LocalServiceApp(world_state_paths=[str(assets/'local_service_world_state.json')],world_paths=[str(assets/'world.json')])
    for p in points:assert services.get_service_detail(**p)['found']
    public=json.dumps([result,routes,services.get_service_detail(**points[1])])
    for forbidden in ['place_id','route_id','service_id','correct_answer']:assert forbidden not in public
    assert before=={p:p.read_bytes() for p in before}
