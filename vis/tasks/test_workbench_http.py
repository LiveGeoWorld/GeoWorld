"""Exercise relocated review behavior against isolated copies of real examples."""
import io,json,shutil,os
from pathlib import Path
from types import SimpleNamespace
import pytest
from vis.tasks import workbench_http as module
from vis.tasks.server import manifest,task_detail
from vis.tasks.evaluation_http import _approved_task_count

@pytest.fixture
def review(tmp_path,monkeypatch):
    if os.name=='nt':tmp_path=Path('\\\\?\\'+str(tmp_path.resolve()))
    project=Path(__file__).resolve().parents[2]
    source=next((project/'examples/workbench/benchmarks/place_grounding').rglob('task.json')).parent
    if os.name=='nt':source=Path('\\\\?\\'+str(source.resolve()))
    rel=Path('place_grounding/world_002/cape_town_south_africa/current_example/tasks/task_0001')
    root=tmp_path/'benchmarks';folder=root/rel;shutil.copytree(source,folder)
    src=tmp_path/'sources/grounding.json';src.parent.mkdir(parents=True)
    document=json.loads((folder/'curated_source.json').read_text(encoding='utf-8'))
    src.write_text(json.dumps({'examples':[document['example']]}),encoding='utf-8')
    document['source_path']=str(src);(folder/'curated_source.json').write_text(json.dumps(document),encoding='utf-8')
    monkeypatch.setattr(module,'WORKSPACE_ROOT',tmp_path)
    handler=SimpleNamespace(active_world_id='world_demo',map_scope={'city_id':'london_smoke'})
    monkeypatch.setattr(module.WorkbenchHTTP,'_current_world_cities',lambda self:[])
    api=module.WorkbenchHTTP(handler,root,{'folder':rel.as_posix()});out=[]
    api._json=lambda data:out.append((200,data));api._error=lambda text,status:out.append((status,text))
    def post(path,body):
        raw=json.dumps(body).encode();handler.headers={'Content-Length':str(len(raw))};handler.rfile=io.BytesIO(raw);api.handle('POST',path);return out[-1]
    return api,post,root,folder,src

def test_review_approval_and_prompt_save_use_same_batch_key(review):
    api,post,root,folder,src=review
    assert post('/api/task/review',{'status':'approved','notes':'Checked.'})[0]==200
    assert _approved_task_count(folder.parent)==1
    rows=manifest(root,{'world':'world_002'})['tasks'];assert rows[0]['review_status']=='approved'
    before=(folder/'grounding_gold.json').read_bytes();prompt=json.loads((folder/'task.json').read_text(encoding='utf-8'))['user_prompt']+' Please explain your choice.'
    assert post('/api/task/save',{'task_type':'place_grounding','fields':{'user_prompt':prompt}})[0]==200
    assert _approved_task_count(folder.parent)==0
    assert task_detail(root,api.params['folder'])['task']['user_prompt']==prompt
    assert (folder/'grounding_gold.json').read_bytes()==before
    assert manifest(root,{'world':'world_002'})['tasks'][0]['review_status']=='needs_fix'

def test_existing_edit_contract_rejects_extra_gold_fields(review):
    api,post,root,folder,src=review
    before=(folder/'grounding_gold.json').read_bytes()
    assert post('/api/task/save',{'task_type':'place_grounding','fields':{'gold':{'wrong':'value'}}})[0]==400
    assert (folder/'grounding_gold.json').read_bytes()==before

def test_source_and_judge_save_sync_existing_local_source(review):
    api,post,root,folder,src=review
    original=json.loads(src.read_text(encoding='utf-8'))['examples'][0]
    assert post('/api/task/source',{'example':{'example_id':'must-not-replace-id','review_status':'pending'}})[0]==200
    updated=json.loads(src.read_text(encoding='utf-8'))['examples'][0]
    assert updated['example_id']==original['example_id'] and updated['review_status']=='pending'
    payload={'stages':[],'completion_policy':'key_nodes'}
    code,result=post('/api/task/judge',payload);assert code==200 and result['source_synced']
    assert json.loads(src.read_text(encoding='utf-8'))['examples'][0]['judge_sequence_override']['stages']==[]

def test_source_sync_cannot_write_outside_release_root(review,tmp_path):
    api,post,root,folder,src=review
    # A sibling of the allowed root is genuinely outside, including on Windows.
    module_root=module.WORKSPACE_ROOT
    outside=module_root.parent/'outside_source.json';outside.write_text('{}')
    record=json.loads((folder/'curated_source.json').read_text(encoding='utf-8'));record['source_path']=str(outside);(folder/'curated_source.json').write_text(json.dumps(record))
    try:
        assert post('/api/task/source',{'example':{'review_status':'approved'}})[0]==400
        assert outside.read_text()=='{}'
    finally:outside.unlink()

def test_old_display_translation_does_not_hide_new_prompt(tmp_path):
    root=tmp_path/'benchmarks';folder=root/'route_planning/world_002/city/batch/tasks/task_0001';folder.mkdir(parents=True)
    (folder/'task.json').write_text(json.dumps({'task_id':'unit','task_type':'route_planning','user_prompt':'Newly edited request.'}))
    (folder/'display_translation.json').write_text(json.dumps({'source_user_prompt':'Old request.','user_prompt':'Old translation.'}))
    assert task_detail(root,folder.relative_to(root).as_posix())['task']['user_prompt']=='Newly edited request.'
    assert manifest(root,{'world':'world_002'})['tasks'][0]['query_preview']=='Newly edited request.'

def test_real_demo_map_and_routes_follow_existing_filters(monkeypatch):
    project=Path(__file__).resolve().parents[2];bundle=project/'examples/world_studio/registered_world'
    record=json.loads(next((bundle/'cities').glob('*.json')).read_text(encoding='utf-8'))
    world=bundle/'assets/world.json';route=bundle/'artifacts/routes/london_smoke/v1/route_cache.json'
    monkeypatch.setattr(module.WorkbenchHTTP,'_current_world_cities',lambda self:[])
    api=module.WorkbenchHTTP(SimpleNamespace(active_world_id='world_demo',map_scope={'city_id':'london_smoke'}),project,{})
    api._resolve_world_paths=lambda:[world];api._resolve_route_cache=lambda city_id='':route
    out=[];api._json=lambda d:out.append(d)
    for endpoint in ['/api/world/full','/api/world/points','/api/route_cache','/api/routes','/api/cached_places']:
        api.handle('GET',endpoint);assert out[-1]
    cache=module._get_route_cache(route)['routes'];first=next(iter(cache.values()))
    api.params={'from':first['origin_place_id'],'to':first['dest_place_id'],'mode':first['mode']}
    api.handle('GET','/api/routes')
    assert out[-1]['routes'] and all(r['origin_place_id']==first['origin_place_id'] and r['dest_place_id']==first['dest_place_id'] and r['mode']==first['mode'] for r in out[-1]['routes'])


def test_edited_sandbox_is_not_overridden_by_old_display_projection(tmp_path):
    import hashlib
    root=tmp_path/'benchmarks';folder=root/'itinerary/world_002/city/batch/tasks/task_0001';folder.mkdir(parents=True)
    (folder/'task.json').write_text(json.dumps({'task_id':'unit','task_type':'itinerary','user_prompt':'Request.'}))
    sandbox=folder/'task_sandbox.json';sandbox.write_text(json.dumps({'expected_feasible':True}))
    h=hashlib.sha256(sandbox.read_bytes()).hexdigest()
    (folder/'workbench_projection.json').write_text(json.dumps({'task_sandbox':{'expected_feasible':True},'source_sandbox_sha256':h}))
    sandbox.write_text(json.dumps({'expected_feasible':False}))
    assert task_detail(root,folder.relative_to(root).as_posix())['task_sandbox']['expected_feasible'] is False
