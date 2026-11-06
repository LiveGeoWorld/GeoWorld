import json,shutil
from pathlib import Path
import pytest
from vis.demo import ROOT,prepare_demo
from vis.tasks.app_episode import query_episode

REF='dynamic_replanning/world_demo/london_smoke/app_demo/tasks/task_0001'

@pytest.fixture
def recorded(tmp_path,monkeypatch):
    for relative in ['examples/world_studio/registered_world','examples/app_runtime/benchmarks/dynamic_replanning/world_demo','examples/app_runtime/benchmarks/multi_entity/world_demo']:
        shutil.copytree(ROOT/relative,tmp_path/relative)
    (tmp_path/'data/manifests').mkdir(parents=True)
    shutil.copy2(ROOT/'data/manifests/app_demo.json',tmp_path/'data/manifests/app_demo.json')
    prepare_demo(tmp_path);monkeypatch.chdir(tmp_path)
    return tmp_path/'examples/app_runtime/benchmarks'


def query(root,app,tool,args=None,node=0,event_mode='shared_world',world='world_demo'):
    return query_episode(root,REF,world,app,tool,args or {},node=node,event_mode=event_mode)


def test_recorded_calendar_contact_message_and_state_reset(recorded):
    before={p:p.read_bytes() for p in recorded.rglob('*.json')}
    cal=query(recorded,'CalendarApp','get_availability',{'character_id':'Sebastian Moore'})
    assert cal['observation']['windows']==[{'start':'18:23','end':'20:30','is_free':True}]
    initial=query(recorded,'MessagingApp','get_contact',{'contact_name':'Sebastian Moore'})['observation']
    later=query(recorded,'MessagingApp','get_contact',{'contact_name':'Sebastian Moore'},node=1)['observation']
    assert initial['dietary']=='vegetarian' and initial['recent_messages']==[]
    assert later['dietary']=='none' and len(later['recent_messages'])==1
    again=query(recorded,'MessagingApp','get_contact',{'contact_name':'Sebastian Moore'})['observation']
    assert again==initial
    assert before=={p:p.read_bytes() for p in before}
    public=json.dumps([cal,initial,later])
    for private in ['exp_1784862900','participant_updates','correct_answer','counterfactual_proof']:
        assert private not in public


def test_recorded_closure_and_dynamic_query_methods(recorded):
    place={'name':'Bonne Bouche Paddington, Coffee Shop & Takeaway','location':{'lat':51.516573699999995,'lng':-0.1735263}}
    initial=query(recorded,'LocalServiceApp','get_service_detail',place)['observation']
    later=query(recorded,'LocalServiceApp','get_service_detail',place,node=2)['observation']
    assert initial.get('open_status')!='closed' and later['open_status']=='closed'
    assert query(recorded,'WeatherApp','get_alerts',node=2)['observation']['count']==0
    assert query(recorded,'CityNewsApp','get_info',node=2)['observation']==[]
    assert query(recorded,'CalendarApp','get_weekday',{'date':'2026-11-27'})['observation']['weekday']=='Friday'


def test_shared_world_and_task_only_use_existing_event_selection(recorded):
    path=recorded/'dynamic_replanning/world_demo/london_smoke/app_demo/city_event_ledger.json'
    ledger=json.loads(path.read_text(encoding='utf8'))
    # Explicit synthetic unit-test fixture only; never part of the distributed demo.
    ledger['events'].append({'event_id':'unit_weather','app':'WeatherApp','world_binding':'world_demo/london_smoke/v1','scope':{'type':'district','id':'unit_scope'},'public_scope':{'name':'Covent Garden'},'activation_time':'2026-11-27T12:25:00+00:00','end_time':'2026-11-27T13:00:00+00:00','state_field':'weather.alert_status','state_patch':{'condition':'rain'},'public_message':'Fixture weather notice.'})
    path.write_text(json.dumps(ledger),encoding='utf8')
    shared=query(recorded,'WeatherApp','get_alerts',node=2,event_mode='shared_world')
    own=query(recorded,'WeatherApp','get_alerts',node=2,event_mode='task_only')
    assert shared['observation']['count']==1 and own['observation']['count']==0
    assert len(shared['nodes'])==5 and len(own['nodes'])==3
    expired=query(recorded,'WeatherApp','get_alerts',node=4,event_mode='shared_world')
    assert expired['time']=='2026-11-27T13:00:00+00:00' and expired['observation']['count']==0


def test_wrong_world_write_methods_and_bad_node_rejected(recorded):
    with pytest.raises(ValueError,match='different world'):query(recorded,'MessagingApp','search_contacts',world='other')
    with pytest.raises(ValueError,match='read methods'):query(recorded,'MessagingApp','send_message',{'recipient_id':'x','content':'x'})
    with pytest.raises(ValueError,match='checkpoint'):query(recorded,'WeatherApp','get_alerts',node=100)


def test_frozen_asset_corruption_is_not_empty_success(recorded):
    source=Path.cwd()/'outputs/worlds/world_demo/assets/world.json';source.write_text('{}')
    with pytest.raises(ValueError,match='hash mismatch'):query(recorded,'MessagingApp','search_contacts')
