from shared.io import read_json
from app.assets.benchmark_layout import latest_batch, task_dirs
from app.apps.safety_event_view import load_safety_event_view
'Read existing latest-batch public Safety views; never generate or rewrite data.'

def static_events(root, world_id, cities):
    apps = {name: [] for name in ('WeatherApp', 'CityNewsApp')}
    sources = []
    for city in cities:
        source = dict(city_id=city['city_id'], city_name=city.get('city_name', city['city_id']), batch='', count=0, status='no_batch')
        sources.append(source)
        try:
            batch = latest_batch(root, 'safety_planning', world_id, city['city_id'])
            if batch is None:
                continue
            source['batch'] = batch.name
            sync_path = batch / 'safety_view_sync.json'
            sync = read_json(sync_path) if sync_path.exists() else None
            if sync is not None and sync.get('status') != 'completed':
                raise ValueError('最新批次公开事件同步尚未完成。')
            shared = None
            hashes = set()
            owners = set()
            for folder in task_dirs(batch):
                task = read_json(folder / 'task.json')
                owners.add(task.get('task_id'))
                view = load_safety_event_view(folder)
                if view is None:
                    raise ValueError('最新批次缺少共享公开事件视图；未回退历史批次。')
                binding = read_json(folder / 'task_sandbox.json')['safety_event_view']
                payload = read_json(folder / binding['path'])
                hashes.add(payload['content_hash'])
                if shared is not None and shared != view:
                    raise ValueError('最新批次的共享公开事件副本不一致。')
                shared = view
            if sync is not None and (hashes != {sync.get('content_hash')} or owners != set(sync.get('committed_task_ids', [])) or owners != set(sync.get('completed_task_ids', []))):
                raise ValueError('最新批次公开事件与同步记录不一致。')
            for name in apps:
                for event in (shared or {}).get(name, []):
                    apps[name].append(dict(content=event, city=source['city_name'], batch=batch.name))
                    source['count'] += 1
            source['status'] = 'ready'
        except (OSError, ValueError, KeyError, TypeError) as exc:
            source.update(status='error', count=None, error=str(exc))
    return dict(world_id=world_id, mode='static', apps=apps, sources=sources)
