import hashlib
import json
from copy import deepcopy

import pytest

from test_runner.eval import itinerary_identity
from test_runner.eval.itinerary_choice import check_itinerary_choice
from test_runner.eval.test_itinerary_choice import _gt, _answer, _result
from test_runner.runner import _evaluate_itinerary


def setup_world(tmp_path, monkeypatch, extra=()):
    monkeypatch.setattr(itinerary_identity, 'ROOT', tmp_path)
    places=[dict(p,city_id='city') for p in _gt()['all_pois']] + list(extra)
    map_path=tmp_path/'bound.json';map_path.write_text(json.dumps({'places':places}))
    sha=hashlib.sha256(map_path.read_bytes()).hexdigest()
    registry=tmp_path/'outputs/worlds/w/cities/city.json';registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({'city_id':'city','city_versions':[
        {'version':1,'artifacts':{'map_world':{'path':'bound.json','sha256':sha}}},
        {'version':2,'artifacts':{'map_world':{'path':'must-not-read-latest.json'}}}]}))
    task=tmp_path/'task';task.mkdir()
    prov={'world_id':'w','city_id':'city','generation_context':{
        'world_city_versions':{'city':1,'other_city':99},
        'world_map_snapshot':{'city':{'version':1,'sha256':sha}}}}
    (task/'task_provenance.json').write_text(json.dumps(prov))
    return task,map_path


def alternative(city='city',identity='outside'):
    return {'place_id':identity,'city_id':city,'name':'Outside Cafe','location':{'lat':11,'lng':12}}


def submitted_alternative():
    answer=_answer();answer['plan']['days'][0]['selected_pois'][0]['selection']={
        'name':'Outside Cafe','location':{'lat':11,'lng':12}}
    return _result(answer)


def test_real_world_alternative_resolves_but_does_not_become_acceptable(tmp_path,monkeypatch):
    task,_=setup_world(tmp_path,monkeypatch,[alternative()]);gt=_gt();before=deepcopy(gt)
    result=submitted_alternative();_evaluate_itinerary(result,gt,task_dir=task)
    assert result.grade=='FAIL'
    assert result.metrics[0].detail=='alternative_unverifiable:public_requirements_missing'
    assert result.metrics[0].verification_status=='unverifiable'
    assert gt==before
    assert result.evaluation_world_snapshot['itinerary_identity']['city_version']==1


def test_existing_correct_answer_uses_exact_bound_version(tmp_path,monkeypatch):
    task,_=setup_world(tmp_path,monkeypatch)
    assert check_itinerary_choice(_result(_answer()),_gt(),task_dir=task).pass_


@pytest.mark.parametrize('extra',[
    [alternative(city='other_city')],
    [alternative(),alternative(identity='ambiguous')],
])
def test_other_city_or_ambiguous_places_are_not_resolved(tmp_path,monkeypatch,extra):
    task,_=setup_world(tmp_path,monkeypatch,extra)
    metric=check_itinerary_choice(submitted_alternative(),_gt(),task_dir=task)
    assert 'place_not_uniquely_resolved' in metric.detail


def test_coordinate_tolerance_is_unchanged(tmp_path,monkeypatch):
    task,_=setup_world(tmp_path,monkeypatch,[alternative()]);result=submitted_alternative()
    result.final_answer['plan']['days'][0]['selected_pois'][0]['selection']['location']['lat']+=.001
    assert 'place_not_uniquely_resolved' in check_itinerary_choice(result,_gt(),task_dir=task).detail


def test_changed_map_is_infrastructure_error_not_model_failure(tmp_path,monkeypatch):
    task,path=setup_world(tmp_path,monkeypatch)
    itinerary_identity.load_identity_places(task)
    path.write_text(path.read_text()+' ')
    with pytest.raises(ValueError,match='hash mismatch'):
        check_itinerary_choice(_result(_answer()),_gt(),task_dir=task)


def test_missing_provenance_does_not_silently_use_task_local_pool(tmp_path):
    with pytest.raises(FileNotFoundError):
        check_itinerary_choice(_result(_answer()),_gt(),task_dir=tmp_path/'missing')


@pytest.mark.parametrize('mutation,match',[
    ('snapshot_version','snapshot version mismatch'),
    ('bound_version','bound map version is unavailable'),
    ('missing_hash','frozen map hash is missing'),
])
def test_invalid_binding_never_selects_another_version(tmp_path,monkeypatch,mutation,match):
    task,_=setup_world(tmp_path,monkeypatch)
    p=task/'task_provenance.json';prov=json.loads(p.read_text())
    if mutation=='snapshot_version':
        prov['generation_context']['world_map_snapshot']['city']['version']=2
    elif mutation=='bound_version':
        prov['generation_context']['world_city_versions']['city']=3
        prov['generation_context']['world_map_snapshot']['city']['version']=3
    else:
        prov['generation_context']['world_map_snapshot']['city'].pop('sha256')
        reg=tmp_path/'outputs/worlds/w/cities/city.json';data=json.loads(reg.read_text())
        data['city_versions'][0]['artifacts']['map_world'].pop('sha256');reg.write_text(json.dumps(data))
    p.write_text(json.dumps(prov))
    with pytest.raises(ValueError,match=match):
        itinerary_identity.load_identity_places(task)
