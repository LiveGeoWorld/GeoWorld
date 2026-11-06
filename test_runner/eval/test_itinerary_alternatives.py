from copy import deepcopy

import pytest

from test_runner.eval.itinerary_alternatives import verify_alternative, _check_fact
from test_runner.schema import AgentRunResult, ToolCallRecord


def example():
    def event(kind,pid,start,end,**extra):
        return dict(block_kind=kind,place_id=pid,start=start,end=end,**extra)
    places={pid:dict(place_id=pid,name=pid,location={'lat':i+1,'lng':2},map_projection={})
            for i,pid in enumerate(['h','a','b'])}
    req={'day':1,'weekday':'Tuesday','level':'required_category','activity_direction':'restaurant',
         'primary_service_type':'restaurant','constraints':[
             {'type':'primary_type','operator':'equals','value':'italian_restaurant'},
             {'type':'price_target_amount','operator':'supports_spend','value':95,'currency':'USD'}]}
    fact={'place_id':'b','service_type':'restaurant','primary_service_type':'restaurant',
          'service_types':['restaurant'],'primary_type':'italian_restaurant',
          'price_range':{'currency':'USD','minimum':50,'maximum':100},
          'weekly_opening_intervals':{'2':[[480,1320]]}}
    events=[event('fixed_event','h','09:00','09:10'),
            event('transit',None,'09:10','09:20',from_place_id='h',to_place_id='a',mode='WALK'),
            event('poi_activity','a','09:20','10:00'),
            event('transit',None,'10:00','10:10',from_place_id='a',to_place_id='h',mode='WALK'),
            event('fixed_event','h','10:10','10:20')]
    gt={'public_requirements':[req],'correct_itinerary':{'days':[{'day':1,'events':events}]},
        'route_data':{'h__TO__b':{'WALK':8},'b__TO__h':{'WALK':9}},'hotel':places['h']}
    result=AgentRunResult('t','','')
    return result,gt,[('b',)],[fact],places


def run(data):
    result,gt,actual,facts,places=data
    return verify_alternative(result,gt,actual,facts=facts,places=places)


def test_different_combination_can_pass_with_complete_evidence():
    data=example();before=deepcopy(data[1:])
    report=run(data)
    assert report['status']=='verified' and report['timeline_certificate']
    assert data[1:]==before


@pytest.mark.parametrize('kind,value,actual',[
    ('primary_type','italian_restaurant','cafe'),
    ('district_name','Center','West'),
    ('price_target_amount',95,{'currency':'USD','minimum':40,'maximum':90}),
    ('price_maximum',100,{'currency':'USD','minimum':50,'maximum':120}),
])
def test_explicit_constraint_violation(kind,value,actual):
    data=example();constraint={'type':kind,'operator':{'primary_type':'equals','district_name':'equals',
        'price_target_amount':'supports_spend','price_maximum':'at_most'}[kind],'value':value,'currency':'USD'}
    data[1]['public_requirements'][0]['constraints']=[constraint]
    data[3][0]['price_range' if kind.startswith('price') else kind]=actual
    report=run(data)
    assert report['status']=='violated'
    assert report['issues'][0]['code']==kind


@pytest.mark.parametrize('mutation', ['missing_price','unknown_operator','unknown_requirement',
    'missing_routes','longer_routes','closed_witness','unknown_hours','missing_weekday','bad_days'])
def test_insufficient_evidence_never_becomes_pass_or_proven_infeasible(mutation):
    data=example();req=data[1]['public_requirements'][0]
    if mutation=='missing_price':data[3][0]['price_range']={}
    if mutation=='unknown_operator':req['constraints'][0]['operator']='approximately'
    if mutation=='unknown_requirement':req['constraints'].append({'type':'magic','operator':'equals','value':1})
    if mutation=='missing_routes':data[1]['route_data']={}
    if mutation=='longer_routes':data[1]['route_data']['h__TO__b']['WALK']=15
    if mutation=='closed_witness':data[3][0]['weekly_opening_intervals']={'2':[[1000,1320]]}
    if mutation=='unknown_hours':data[3][0]['weekly_opening_intervals']={}
    if mutation=='missing_weekday':req.pop('weekday')
    if mutation=='bad_days':req['day']=2
    assert run(data)['status']=='unverifiable'


def test_verified_observations_can_supply_missing_route_evidence():
    data=example();data[1]['route_data']={};r=data[0];p=data[4]
    for i,(a,b) in enumerate([('h','b'),('b','h')]):
        r.trace.append(ToolCallRecord(i,'','MapApp__query_routes',
            args={'origin':p[a],'destination':p[b]},
            result={'routes':[{'mode':'WALK','available':True,'duration_s':480}]}))
    assert run(data)['status']=='verified'
    r.trace[1].result['routes'][0]['available']=False
    assert run(data)['status']=='unverifiable'


def test_conflicting_route_facts_are_unknown_not_fastest_route():
    data=example();p=data[4]
    data[0].trace.append(ToolCallRecord(1,'','MapApp__query_routes',
        args={'origin':p['h'],'destination':p['b']},
        result={'routes':[{'mode':'WALK','available':True,'duration_s':10}]}))
    assert run(data)['status']=='unverifiable'


def test_public_route_range_checks_actual_chosen_previous_place():
    data=example();req=data[1]['public_requirements'][0]
    req['activity_group']={'start_anchor':{'kind':'hotel','place_name':'h'}}
    req['constraints'].append({'type':'route_duration_range','operator':'between','mode':'WALK',
                              'relation':'start_anchor_to_gt','minimum':1,'maximum':6})
    report=run(data)
    assert report['status']=='violated' and report['issues'][0]['code']=='route_duration_range'


def test_missing_service_flag_is_unknown_explicit_false_is_violation():
    c={'type':'service_feature','operator':'includes','value':'reservable'}
    assert _check_fact({'service_features':[]},c,2)[0] is None
    assert _check_fact({'_service_details':{'reservable':False}},c,2)[0] is False


def test_private_personal_activity_not_rewritten_to_another_venue():
    data=example();data[1]['correct_itinerary']['days'][0]['events'].insert(3,
        {'block_kind':'personal_activity','place_id':'a','start':'10:00','end':'10:00'})
    report=run(data)
    assert report['status']=='unverifiable'
    assert report['issues'][0]['code']=='personal_activity_location_unverifiable'


def test_unavailable_observation_cannot_be_overridden_by_cached_positive_duration():
    data=example();p=data[4]
    data[0].trace.append(ToolCallRecord(1,'','MapApp__query_routes',
        args={'origin':p['h'],'destination':p['b']},
        result={'routes':[{'mode':'WALK','available':False,'duration_s':0}]}))
    assert run(data)['status']=='unverifiable'


def test_factual_service_projection_keeps_secondary_capabilities(tmp_path,monkeypatch):
    import json
    from test_runner.eval import itinerary_alternatives as mod
    p={'place_id':'multi','city_id':'c','name':'Cafe Restaurant','primary_type':'fast_food_restaurant',
       'location':{'lat':1,'lng':2},'map_projection':{'types':['restaurant','cafe','fast_food_restaurant']}}
    world={'places':[p],'services':[{'place_id':'multi','service_id':'s','service_type':'restaurant'}]}
    path=tmp_path/'world.json';path.write_text(json.dumps(world))
    monkeypatch.setattr(mod,'load_identity_places',lambda _:([p],{'path':str(path),'city_id':'c'}))
    facts,places=mod.frozen_facts(tmp_path)
    assert {'restaurant','cafe'}.issubset({f['service_type'] for f in facts})
    assert all({'restaurant','cafe'}.issubset(set(f['service_types'])) for f in facts)


def test_end_to_end_metric_can_accept_an_alternative(monkeypatch):
    from test_runner.eval import itinerary_alternatives as mod, itinerary_identity
    from test_runner.eval.itinerary_choice import check_itinerary_choice
    data=example();result,gt,actual,facts,places=data
    gt.update(expected_feasible=True,num_days=1,all_pois=list(places.values()))
    result.final_answer={'feasible':True,'explanation':'A complete alternative.',
        'plan':{'days':[{'day':1,'selected_pois':[{'activity_order':1,'selection':{
            'name':'b','location':places['b']['location']}}]}]}}
    monkeypatch.setattr(itinerary_identity,'load_identity_places',lambda _:(list(places.values()),{}))
    monkeypatch.setattr(mod,'frozen_facts',lambda _:(facts,places))
    metric=check_itinerary_choice(result,gt,task_dir='bound')
    assert metric.pass_ and '替代行程' in metric.detail
    assert result.evaluation_world_snapshot['itinerary_alternative']['status']=='verified'


def test_rounded_route_boundary_is_unknown_not_a_false_violation():
    data=example();req=data[1]['public_requirements'][0]
    req['activity_group']={'start_anchor':{'kind':'hotel','place_name':'h'}}
    req['constraints'].append({'type':'route_duration_range','operator':'between','mode':'WALK',
                              'relation':'start_anchor_to_gt','minimum':9,'maximum':9})
    report=run(data)
    assert report['status']=='unverifiable'
    assert all(i['status']!='violated' for i in report['issues'])


def test_global_budget_cannot_be_silently_ignored():
    data=example();data[1]['constraints']={'max_daily_budget':50}
    report=run(data)
    assert report['status']=='unverifiable'
    assert report['issues'][0]['code']=='additional_global_constraints_unverifiable'


def test_alternative_can_shift_activity_inside_fixed_anchors():
    data=example()
    # Original visit starts at 09:20, but the alternative opens at 09:30.
    data[3][0]['weekly_opening_intervals']={'2':[[570,1320]]}
    data[1]['correct_itinerary']['days'][0]['events'][-1].update(start='10:30',end='10:40')
    before=deepcopy(data[1:])
    report=run(data)
    assert report['status']=='verified'
    certificate=report['timeline_certificate'][0]
    assert certificate['method']=='fixed_selection_rescheduled'
    visit=next(e for e in certificate['events'] if e['block_kind']=='poi_activity')
    assert visit['place_id']=='b' and visit['start_s']==570*60
    assert visit['end_s']-visit['start_s']==40*60
    assert data[1:]==before


def test_rescheduling_does_not_invent_missing_routes_or_move_fixed_event():
    data=example()
    data[3][0]['weekly_opening_intervals']={'2':[[570,1320]]}
    assert run(data)['status']=='unverifiable'  # Cannot reach fixed 10:10 anchor.
    data[1]['correct_itinerary']['days'][0]['events'][-1].update(start='10:30',end='10:40')
    data[1]['route_data']={}
    assert run(data)['status']=='unverifiable'
