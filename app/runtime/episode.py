"""Formal App assembly shared by test execution and the author workbench."""
from pathlib import Path
from shared.io import read_json
from app.runtime.asset_bindings import _resolve_place_world_snapshot
from app.runtime.environment import Environment
from app.runtime.contracts import load_agent_environment
from app.apps.map_app import MapAppSandbox
from app.apps.local_service import LocalServiceApp
from app.apps.messaging import MessagingApp
from app.apps.calendar import CalendarApp
from app.apps.weather_app import WeatherApp
from app.apps.city_news import CityNewsApp
from app.apps.user_interface import UserInterfaceApp


def load_episode(task_dir, *, route_cache_path='', world_path='', dynamic_event_mode='shared_world', dynamic_information_mode='pull', task_data=None, gt_data=None):
    from app.runtime.shared_events import validate_event_mode
    from app.runtime.explicit_updates import validate_information_mode
    validate_event_mode(dynamic_event_mode)
    validate_information_mode(dynamic_information_mode)
    task_dir=Path(task_dir)
    task_data=read_json(task_dir/'task.json') if task_data is None else task_data
    task_type=task_data['task_type']
    sandbox=task_dir/('route_sandbox.json' if task_type=='route_planning' else 'task_sandbox.json')
    gt_data=(read_json(sandbox) if sandbox.exists() else {}) if gt_data is None else gt_data
    environment_contract=load_agent_environment(task_dir,task_type)
    map_world_binding={}
    event_snapshot={}
    try:
        map_world_binding = _resolve_place_world_snapshot(world_path, task_dir, task_data)
        map_world_paths = map_world_binding["world_paths"]
        local_service_world_state_paths = map_world_binding.get("local_service_world_state_paths", [])
    except FileNotFoundError:
        # Historical tasks without provenance retain a route-cache-only read
        # path. New formal tasks must provide the frozen world binding.
        map_world_paths = []
        local_service_world_state_paths = []
    # Safety formal tasks carry a task-bound route-cache projection.  It is
    # part of the frozen task input and must be visible to MapApp in addition
    # to the world-level cache; otherwise valid task routes are invisible at
    # evaluation time.
    task_route_cache = {}
    if task_type == "safety_planning":
        task_route_cache = ((gt_data.get("safety_choice") or {}).get("route_cache") or {})
    env = Environment()
    env.dynamic_information_mode = dynamic_information_mode
    env.register_app(MapAppSandbox(
        route_cache_path=route_cache_path,
        world_paths=map_world_paths,
        route_cache_data=task_route_cache if isinstance(task_route_cache, dict) else None,
    ))
    env.register_app(LocalServiceApp(
        str(task_dir), world_state_paths=local_service_world_state_paths, world_paths=map_world_paths,
    ))
    env.register_app(MessagingApp(str(task_dir)))
    env.register_app(CalendarApp(str(task_dir)))
    env.register_app(WeatherApp(str(task_dir)))
    env.register_app(CityNewsApp(str(task_dir)))
    if task_type == "dynamic_replanning":
        from app.apps.dynamic_user_interface import DynamicUserInterfaceApp
        env.register_app(DynamicUserInterfaceApp())
    else:
        env.register_app(UserInterfaceApp())
    env.configure_application_catalog(
        environment_contract["applications"],
        always_available_tools={("UserInterfaceApp", "final_answer")},
    )

    # Dynamic tasks use a canonical event envelope. Legacy bare lists remain readable.
    if task_type == "dynamic_replanning":
        env_events_path = task_dir / "env_events.json"
        if env_events_path.exists():
            task_events = read_json(env_events_path)
        else:
            task_events = gt_data.get("events", gt_data.get("disruptions", []))
        from app.runtime.shared_events import load_world_events
        world_events, own_events, event_snapshot = load_world_events(
            task_dir, task_events, dynamic_event_mode=dynamic_event_mode)
        env.load_dynamic_events(
            world_events,
            required_event_ids={event["event_id"] for event in own_events},
        )

        # Pre-v1 tasks provide `timestep`; v1 tasks provide `time_context`.
        time_context = gt_data.get("time_context", task_data.get("time_context", {}))
        if isinstance(time_context, dict):
            task_timestep = time_context.get("episode_start", time_context.get("instruction_time", ""))
        else:
            task_timestep = ""
        task_timestep = task_timestep or gt_data.get("timestep", "")
        source_kind = (task_data.get("summary") or {}).get("base_task_type")
        from app.runtime.planning_window import decision_deadline
        source_kind = source_kind or (gt_data.get("source_task") or {}).get("task_type", "")
        deadline = decision_deadline(time_context, source_kind, gt_data)
        env.init_events(task_timestep, planning_deadline=deadline)
        env.dynamic_source_kind = source_kind
        event_snapshot["decision_window"] = {"start_time": task_timestep, "end_time": deadline}
        gt_data["_dynamic_events"] = own_events

    return {'env':env,'binding':map_world_binding,'task':task_data,'sandbox':gt_data,'events':event_snapshot}


def event_checkpoints(env):
    """Expose recorded activation/expiry transitions inside the formal time window."""
    parse=env.time_manager._parse_time
    current=env.time_manager.virtual_time()
    points=set()
    for event in env.disruptions:
        for key in ('activation_time','end_time'):
            value=event.get(key)
            if not value:continue
            point=parse(value)
            if current is not None and point<=current:continue
            if env.planning_deadline is not None and (point>env.planning_deadline or (key=='activation_time' and point==env.planning_deadline)):continue
            points.add(value)
    return sorted(points,key=parse)
