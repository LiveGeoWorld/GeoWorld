"""Regression tests for the migrated existing result and execution endpoints."""
import io
import os
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from shared.io import write_json
from vis.tasks.evaluation_http import EvaluationHTTP
from vis.tasks.result_examples import install_result_examples


def adapter(tmp_path, monkeypatch, body=None):
    from vis.tasks import evaluation_http as module
    monkeypatch.setattr(EvaluationHTTP,'runs_root_override',str(tmp_path/'runs'))
    payload=json.dumps(body or {}).encode()
    handler=SimpleNamespace(active_world_id='world_002',rfile=io.BytesIO(payload),headers={'Content-Length':str(len(payload))})
    result=EvaluationHTTP(handler,tmp_path/'benchmarks',{})
    captured=[]
    result._json=lambda data:captured.append((200,data))
    result._error=lambda message,status:captured.append((status,message))
    return result,captured


def test_result_details_and_delete_keep_real_artifact_layout(tmp_path,monkeypatch):
    api,out=adapter(tmp_path,monkeypatch)
    root=tmp_path/'runs/run_one'
    saved={'task_id':'t','grade':'FAIL','metrics':[{'metric':'answer','pass':False}],
           'trace':[{'tool':'MapApp__query_routes','args':{},'result':{'minutes':5}}],
           'final_answer':{'feasible':False,'explanation':'Recorded answer.'}}
    write_json(root/'results.json',{'run_id':'run_one','tasks':[saved]})
    write_json(root/'tasks/0001.json',saved)
    write_json(root/'tasks/task_index.json',{'entries':[{'task_id':'t','file_name':'0001.json'}]})
    api.params={'run':'run_one'}
    api.handle('GET','/api/run')
    assert out[-1][1]['results'][0]['grade']=='FAIL'
    assert 'trace' not in out[-1][1]['results'][0]['raw']
    api.params['task_id']='t';api.handle('GET','/api/run/task')
    assert out[-1][1]['trajectory']==saved['trace']
    assert out[-1][1]['outcome']['final_answer']==saved['final_answer']
    api.handle('DELETE','/api/run')
    assert not root.exists()
    api.handle('GET','/api/runs')
    assert out[-1][1]['runs']==[]


def test_missing_identifiers_and_traversal_are_rejected(tmp_path,monkeypatch):
    api,out=adapter(tmp_path,monkeypatch)
    api.handle('GET','/api/run');assert out[-1][0]==400
    root=tmp_path/'runs/run_one';root.mkdir(parents=True)
    write_json(tmp_path/'runs/private.json',{'secret':'must not be returned'})
    api.params={'run':'run_one','task_id':'../private.json'}
    api.handle('GET','/api/run/task');assert out[-1][0]==400
    api,out=adapter(tmp_path,monkeypatch,{'run_id':'../outside'})
    api.handle('POST','/api/run/start');assert out[-1][0]==400
    assert not (tmp_path/'outside').exists()


def test_active_run_cannot_be_deleted(tmp_path,monkeypatch):
    api,out=adapter(tmp_path,monkeypatch)
    root=tmp_path/'runs/run_one';write_json(root/'run_state.json',{'status':'running'})
    api.params={'run':'run_one'};api.handle('DELETE','/api/run')
    assert out[-1][0]==409 and root.exists()


def example_root(tmp_path):
    if os.name == "nt":
        tmp_path=Path("\\\\?\\"+str(tmp_path.resolve()))
    project=Path(__file__).resolve().parents[2]
    shutil.copytree(project/'examples/test_results',tmp_path/'examples/test_results')
    target=tmp_path/'data/manifests';target.mkdir(parents=True)
    shutil.copy2(project/'data/manifests/result_samples.json',target/'result_samples.json')
    return tmp_path


def test_example_installation_is_idempotent_and_keeps_user_changes(tmp_path):
    root=example_root(tmp_path)
    assert install_result_examples(root)==1
    output=next((root/'outputs/evaluations').rglob('results.json'))
    output.write_text('{"user_change":true}')
    assert install_result_examples(root)==1
    assert json.loads(output.read_text())=={'user_change':True}


def test_corrupt_result_bundle_is_not_installed(tmp_path):
    root=example_root(tmp_path)
    next((root/'examples/test_results').rglob('results.json')).write_text('{}')
    with pytest.raises(ValueError,match='integrity'):install_result_examples(root)
    assert not (root/'outputs/evaluations').exists()


def test_bundled_run_has_two_real_results_bound_to_reviewed_tasks():
    project=Path(__file__).resolve().parents[2]
    manifest=json.loads((project/'data/manifests/result_samples.json').read_text(encoding='utf-8'))
    assert len(manifest['samples'])==1
    folder=project/manifest['samples'][0]['destination']
    summary=json.loads((folder/'results.json').read_text(encoding='utf-8'))
    assert summary['task_count']==2 and len(summary['tasks'])==2
    assert summary['passed']==sum(row['grade']=='PASS' for row in summary['tasks'])
    assert summary['token_usage']['total']==sum(row['tokens_used']['total'] for row in summary['tasks'])
    assert len({row['task_id'] for row in summary['tasks']})==2
    for row in summary['tasks']:
        matches=[p for p in (project/'examples/workbench/benchmarks').rglob('task.json')
                 if json.loads(p.read_text(encoding='utf-8')).get('task_id')==row['task_id']]
        assert len(matches)==1
        assert row['final_answer'] and row['trace']


def test_positive_model_preflight_uses_imported_url_parser(monkeypatch):
    from vis.tasks import evaluation_http as module
    class Config:
        def __init__(self, path): pass
        def model_endpoint(self, name):
            return {'base_url':'http://127.0.0.1:8317/v1','api_key':'unit-test-only'}
    class Connection:
        def request(self, *args, **kwargs): pass
        def getresponse(self):
            return SimpleNamespace(status=200,read=lambda:b'{"data":[]}')
        def close(self): pass
    monkeypatch.setattr(module,'PipelineConfig',Config)
    monkeypatch.setattr(module,'api_connection',lambda *args,**kwargs:(Connection(),'/v1/models'))
    assert module._preflight_test_model('local','test-only.yaml') is None
