"""Read-only queries against the same assembled episode used by the Runner."""
from pathlib import Path
from shared.io import read_json
from app.runtime.episode import load_episode,event_checkpoints
from app.runtime.asset_bindings import _resolve_place_world_snapshot, _resolve_route_cache
from vis.tasks.app_content import resolve_folder

READS={
 'MapApp':{'search_places','get_place_detail','query_routes'},
 'LocalServiceApp':{'get_service_detail','check_availability','browse_services'},
 'CalendarApp':{'get_weekday','get_festivals','get_holidays','get_availability','check_conflicts'},
 'MessagingApp':{'search_contacts','get_contact','get_user_messages'},
 'WeatherApp':{'get_alerts'},'CityNewsApp':{'get_info'},
 'UserInterfaceApp':{'get_simulation_time'},
}


def episode_at(root,reference,node=0,event_mode='shared_world',world=''):
    folder=resolve_folder(root,reference)
    task=read_json(folder/'task.json')
    relative=folder.relative_to(Path(root).resolve()).parts
    if world and relative[1]!=world:
        raise ValueError('The episode belongs to a different world.')
    # Fail on absent managed assets; do not turn a broken bundle into an empty world.
    _resolve_place_world_snapshot(None,folder,task)
    episode=load_episode(folder,route_cache_path=_resolve_route_cache(None,folder.parent),dynamic_event_mode=event_mode)
    env=episode['env'];now=env.time_manager.virtual_time_iso()
    times=event_checkpoints(env)
    nodes=[{'index':0,'label':'Initial state','time':now}]+[{'index':i,'label':f'Event checkpoint {i}','time':t} for i,t in enumerate(times,1)]
    if type(node) is not int or not 0<=node<len(nodes):
        raise ValueError('Invalid event checkpoint.')
    if node:env.advance_virtual_time(times[node-1],reason='read_only_preview')
    episode['nodes']=nodes;episode['node']=node
    return episode


def query_episode(root,reference,world,app,tool,args,*,node=0,event_mode='shared_world'):
    if app not in READS or tool not in READS[app] or not isinstance(args,dict):
        raise ValueError('This workbench accepts public read methods only.')
    episode=episode_at(root,reference,node,event_mode,world)
    env=episode['env']
    opened=env.execute_tool('SystemApp__open_app',{'app':app})
    if opened.get('status')!='opened':
        raise ValueError('This App is not installed in the recorded episode.')
    if app=='LocalServiceApp' and tool=='browse_services':
        from vis.tasks.app_access import browse_services
        paths=[Path(p) for p in episode['binding']['world_paths']]
        zones={str(p.resolve()):(read_json(p).get('cities') or [{}])[0].get('timezone') for p in paths}
        args={**args,'time':env.time_manager.virtual_time_iso()}
        class PublicReads:
            _public_services=env.apps[app]._public_services
            def get_service_detail(self,name,location):
                return env.execute_tool('LocalServiceApp__get_service_detail',{'name':name,'location':location})
        observation=browse_services([],args,paths,zones,app=PublicReads())
        for row in observation['results']:
            detail=env.execute_tool('LocalServiceApp__get_service_detail',{'name':row['name'],'location':row['location']})
            for key in ['open_status','availability']:
                if detail.get(key) is not None:row[key]=detail[key]
    else:
        observation=env.execute_tool(app+'__'+tool,args)
    return {'world_id':world,'tool':app+'__'+('get_service_detail' if tool=='browse_services' else tool),'arguments':args,'observation':observation,
            'time':env.time_manager.virtual_time_iso(),'node':node,'nodes':episode['nodes'],
            'event_mode':event_mode,'warnings':[]}
