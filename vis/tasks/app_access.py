from app.runtime.environment import Environment
from app.apps.local_service import LocalServiceApp
from app.apps.weather_app import WeatherApp
from app.apps.city_news import CityNewsApp
from vis.tasks.app_events import static_events
'Read-only public App methods, without inventing task/person context.'
READS = {'LocalServiceApp': ('get_service_detail', 'check_availability'), 'WeatherApp': ('get_alerts',), 'CityNewsApp': ('get_info',)}

def regular_service_status(hours, time, zone):
    from datetime import datetime
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    from shared.opening_hours import valid_periods
    if not time:
        return None
    point = datetime.fromisoformat(time.replace('Z', '+00:00'))
    if point.tzinfo is None:
        raise ValueError('查看时间必须包含时区。')
    if not valid_periods(hours):
        return '营业时间未提供或无法计算'
    if not zone:
        return '商户时区未提供'
    try:
        point = point.astimezone(ZoneInfo(zone))
    except ZoneInfoNotFoundError:
        return '商户时区无法识别'
    minute = (point.weekday() + 1) % 7 * 1440 + point.hour * 60 + point.minute
    for period in hours['periods']:
        if 'close' not in period:
            return '营业中'

        def value(key):
            p = period[key]
            return p['day'] * 1440 + p.get('hour', 0) * 60 + p.get('minute', 0)
        (start, end) = (value('open'), value('close'))
        if end <= start:
            end += 10080
        if start <= minute < end or start <= minute + 10080 < end:
            return '营业中'
    return '非营业时间'

def browse_services(paths, args, world_paths=(), world_timezones=None, *, app=None):
    """Paginated UI projection of public service details, not a new Agent tool."""
    app = app if app is not None else LocalServiceApp(world_state_paths=[str(p) for p in paths], world_paths=list(world_paths))
    from shared.io import read_json
    from shared.opening_hours import effective_opening_hours
    from world_module.service_detail_snapshot import identity_key, public_identity
    places = {identity_key(identity): p for path in world_paths for p in read_json(path).get('places', []) if (identity := public_identity(p)) is not None}
    zones = {identity_key(identity): (world_timezones or {}).get(str(path.resolve())) for path in world_paths for p in read_json(path).get('places', []) if (identity := public_identity(p)) is not None}
    time = str(args.get('time', '')).strip()
    if time:
        regular_service_status(None, time, None)
    query = str(args.get('query', '')).strip().casefold()
    rows = sorted((s for s in app._public_services if query in s['name'].casefold()), key=lambda s: (s['name'].casefold(), str(s['location'])))
    (offset, limit) = (max(0, int(args.get('offset', 0))), max(1, min(50, int(args.get('limit', 10)))))
    results = []
    aliases = {'price_level': ('price_level',), 'duration_min': ('duration_typical_min', 'duration_min'), 'opening_hours': ('opening_hours', 'open_until')}
    for service in rows[offset:offset + limit]:
        detail = app.get_service_detail(service['name'], service['location'])
        detail = {k: v for (k, v) in detail.items() if k in ('found', 'name', 'location', 'merchant_information') or any((key in service for key in aliases.get(k, (k,))))}
        if not detail.get('opening_hours'):
            detail['opening_hours'] = effective_opening_hours(places.get(identity_key(service), {}))
        place = places.get(identity_key(service), {})
        projection = place.get('map_projection') or {}
        google = projection.get('google_place_details') or {}
        detail['primary_type'] = projection.get('primary_type') or place.get('primary_type')
        for key in ('description', 'service_description'):
            if isinstance(service.get(key), str) and service[key].strip():
                detail['description'] = service[key]
                break
        detail['website'] = google.get('website_uri')
        detail['regular_status'] = regular_service_status(detail.get('opening_hours'), time, zones.get(identity_key(service)))
        for key in ('rating', 'user_rating_count'):
            if projection.get(key) is not None:
                detail[key] = projection[key]
        for key in ('price_level', 'price_range', 'reservable', 'dine_in', 'takeout', 'delivery', 'curbside_pickup'):
            if google.get(key) is not None and key not in detail:
                detail[key] = google[key]
        if 'price_pp' in service:
            detail['price_per_person'] = {'amount': service['price_pp'], 'currency': service.get('currency')}
        values = {k: detail.pop(k) for k in ('open_status', 'availability', 'queue_time_min', 'crowding_level') if k in detail and detail[k] is not None and (detail[k] != '')}
        detail['state_records'] = [{'values': values, 'active_window': service.get('active_window')}] if values else []
        results.append(detail)
    return dict(total=len(rows), offset=offset, results=results, query_time=time)

def invoke(root, world, cities, service_paths, app_name, tool, args, mode, world_paths=(), world_timezones=None):
    if mode not in ('static', 'dynamic'):
        raise ValueError('无效事件模式。')
    if app_name == 'LocalServiceApp' and tool == 'browse_services' and isinstance(args, dict):
        observation = browse_services(service_paths, args, world_paths, world_timezones)
        from vis.tasks.service_review import add_static_review
        add_static_review(observation, root, world, cities, world_paths, mode)
        if mode == 'dynamic':
            for row in observation['results']:
                row.pop('state_records', None)
                row.pop('slots', None)
        return dict(world_id=world, projection='Public service browser', mode=mode, event_state='review_projection', read_method='LocalServiceApp__get_service_detail', arguments=args, observation=observation, warnings=[])
    if mode != 'static':
        raise ValueError('Select a task runtime to query dynamic events.')
    if app_name == 'WeatherApp' and tool == 'browse_weather' and isinstance(args, dict):
        from app.apps.safety_event_view import matches_public_scope, matches_time_range
        from datetime import datetime
        time = str(args.get('time', '')).strip()
        if time and datetime.fromisoformat(time.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError('查看时间必须包含时区。')
        data = static_events(root, world, cities)
        rows = [r['content'] for r in data['apps']['WeatherApp'] if matches_public_scope(r['content'], str(args.get('district_id', ''))) and matches_time_range(r['content'], time)]
        return dict(world_id=world, projection='Weather records', read_method='Shared public weather events', arguments=args, observation=rows, warnings=[f"{s['city_name']}：{s['error']}" for s in data['sources'] if s['status'] == 'error'])
    if app_name not in READS or tool not in READS[app_name] or (not isinstance(args, dict)):
        raise ValueError('Select a task runtime and a public read method.')
    warnings = []
    if app_name == 'LocalServiceApp':
        app = LocalServiceApp(world_state_paths=[str(p) for p in service_paths], world_paths=list(world_paths))
    else:
        data = static_events(root, world, cities)
        app = WeatherApp() if app_name == 'WeatherApp' else CityNewsApp()
        app._safety_view = [row['content'] for row in data['apps'][app_name]]
        warnings = [f"{s['city_name']}：{s['error']}" for s in data['sources'] if s['status'] == 'error']
    env = Environment()
    env.register_app(app)
    env.configure_public_tools({app_name: list(READS[app_name])})
    name = app_name + '__' + tool
    return dict(world_id=world, tool=name, arguments=args, observation=env.execute_tool(name, args), warnings=warnings)
