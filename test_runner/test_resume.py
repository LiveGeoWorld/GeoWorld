import json
from pathlib import Path
from unittest.mock import patch
import pytest
from shared.io import write_json, read_json
from test_runner import resume


def make_run(tmp_path, count=3):
    run=tmp_path/'run'; (run/'tasks').mkdir(parents=True)
    tasks=tmp_path/'source'/'tasks'; tasks.mkdir(parents=True)
    selected=[]
    for i in range(count):
        p=tasks/f'task_{i}';p.mkdir();write_json(p/'task.json',{'task_id':f'id_{i}','task_type':'itinerary'})
        selected.append({'task_id':f'id_{i}','task_folder':str(p)})
    write_json(run/'run_manifest.json',{'model_key':'test','selected_tasks':selected,'scope':{'task_type':'itinerary'}})
    write_json(run/'run_state.json',{'status':'interrupted','started_at':'old','elapsed_seconds':10,'output_dir':str(run)})
    return run,tasks


def row(tasks,i,reason=None):
    return {'task_id':f'id_{i}','task_type':'itinerary','task_folder':f'task_{i}',
            'source_tasks_dir':str(tasks),'source_batch_id':'source','model_name':'test',
            'grade':'FAIL','tokens_used':{'prompt':3,'completion':2,'total':5},'duration_s':1,
            'termination':reason or {}}


def test_reconcile_preserves_numbers_and_payloads(tmp_path):
    run,tasks=make_run(tmp_path);r=row(tasks,0);k=resume.identity(r)
    write_json(run/'tasks/0042.json',r);original=(run/'tasks/0042.json').read_bytes()
    rows={k:r};files={k:'0042.json'}
    r2=row(tasks,2);rows[resume.identity(r2)]=r2
    resume.reconcile(run,rows,files,status='prepared')
    assert (run/'tasks/0042.json').read_bytes()==original
    assert files[resume.identity(r2)]=='0043.json'
    assert read_json(run/'results.json')['task_count']==3
    assert read_json(run/'run_state.json')['completed_count']==2
    assert read_json(run/'results.json')['token_usage']['total']==10
    assert resume.load_rows(run)[0]==rows


def test_reconcile_rejects_mixed_event_modes_and_retains_mode(tmp_path):
    run,tasks=make_run(tmp_path)
    manifest=read_json(run/'run_manifest.json')
    manifest['dynamic_event_mode']='task_only'
    write_json(run/'run_manifest.json',manifest)
    r=row(tasks,0)
    with pytest.raises(ValueError,match='mixed-mode'):
        resume.reconcile(run,{resume.identity(r):r},{},status='prepared')
    r['dynamic_event_mode']='task_only'
    resume.reconcile(run,{resume.identity(r):r},{},status='prepared')
    assert read_json(run/'results.json')['dynamic_event_mode']=='task_only'


def test_detail_first_crash_and_partial_jsonl_recovery(tmp_path):
    run,tasks=make_run(tmp_path);r=row(tasks,0)
    write_json(run/'tasks/0001.json',r)
    (run/'results.jsonl').write_text('{"incomplete":')
    rows,files=resume.load_rows(run)
    resume.reconcile(run,rows,files,status='prepared')
    assert len(resume.load_rows(run)[0])==1
    assert json.loads((run/'results.jsonl').read_text())==r


def test_conflict_and_duplicate_rejected(tmp_path):
    run,tasks=make_run(tmp_path);r=row(tasks,0)
    write_json(run/'tasks/0001.json',r)
    r2=dict(r,grade='PASS');(run/'results.jsonl').write_text(json.dumps(r2)+'\n')
    with pytest.raises(ValueError,match='Conflicting'):resume.load_rows(run)
    (run/'results.jsonl').write_text((json.dumps(r)+'\n')*2)
    with pytest.raises(ValueError,match='Duplicate'):resume.load_rows(run)


def test_only_api_failures_are_removed():
    assert resume.is_api_failure({'termination':{'reason':'evaluation_infrastructure_failure','category':'model_api'}})
    assert not resume.is_api_failure({'termination':{'reason':'agent_protocol_failure','category':'no_action'}})
    assert not resume.is_api_failure({'termination':{'reason':'evaluation_infrastructure_failure','category':'persistence'}})


def test_pair_barrier_and_failure_stop():
    events=[]
    def start(x):events.append(('start',x));return x
    def wait(x):events.append(('wait',x));return 0
    resume.execute_waves([[1,2],[3,4],[5]],start,wait)
    assert events==[('start',1),('start',2),('wait',1),('wait',2),('start',3),('start',4),('wait',3),('wait',4),('start',5),('wait',5)]
    events.clear()
    with pytest.raises(RuntimeError):resume.execute_waves([[1,2],[3]],start,lambda x:1)
    assert events==[('start',1),('start',2)]


def test_process_identity_and_exclusive_lock(tmp_path):
    state={'resume_worker':resume.process_identity()}
    assert resume.managed_worker_alive(state)
    state['resume_worker']['start_ticks']='wrong'
    assert not resume.managed_worker_alive(state)
    with resume.exclusive(tmp_path/'lock'):
        with pytest.raises(BlockingIOError):
            with resume.exclusive(tmp_path/'lock'):pass


def test_resume_only_missing_and_second_resume_makes_no_calls(tmp_path):
    run,tasks=make_run(tmp_path);r=row(tasks,0)
    write_json(run/'tasks/0007.json',r)
    resume.reconcile(run,{resume.identity(r):r},{resume.identity(r):'0007.json'},status='prepared')
    from test_runner.schema import AgentRunResult
    calls=[]
    def fake(folder,**kwargs):
        calls.append(folder.name)
        r=AgentRunResult(task_id='id_'+folder.name.split('_')[-1],user_prompt='p',gt_route_id='')
        r.task_folder=folder.name;r.task_type='itinerary';r.model_name='test'
        return r
    with patch('test_runner.runner.run_task',fake),patch('test_runner.runner._resolve_route_cache',return_value=''),patch('test_runner.verification.shared.formalization.is_formal_task',return_value=True):
        resume.resume_run(run);resume.resume_run(run)
    assert calls==['task_1','task_2']
    rows,files=resume.load_rows(run)
    assert len(rows)==3 and rows[resume.identity(r)]==r
    assert files[resume.identity(r)]=='0007.json'
    assert read_json(run/'run_state.json')['status']=='completed'


def test_frontend_keeps_live_external_worker_and_marks_dead_one(tmp_path):
    from vis.tasks.evaluation_http import EvaluationHTTP as TaskBrowserHandler
    run,_=make_run(tmp_path)
    state=read_json(run/'run_state.json')
    state.update(status='running',resume_worker=resume.process_identity())
    write_json(run/'run_state.json',state)
    handler=object.__new__(TaskBrowserHandler)
    handler._resolve_run_dir=lambda _:run
    outputs=[];handler._json=outputs.append
    handler._serve_run_status(run.name)
    assert outputs[-1]['status']=='running'
    state['resume_worker']['start_ticks']='dead'
    write_json(run/'run_state.json',state)
    handler._serve_run_status(run.name)
    assert outputs[-1]['status']=='interrupted'


def test_unknown_result_is_rejected(tmp_path):
    run,tasks=make_run(tmp_path);r=row(tasks,99)
    with pytest.raises(ValueError,match='outside'):resume.reconcile(run,{resume.identity(r):r},{},status='prepared')
