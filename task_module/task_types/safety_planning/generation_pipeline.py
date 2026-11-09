"""Formal Safety generation using the confirmed fixed-GT construction chain.
The local matcher is match_activity().
"""

from __future__ import annotations

import copy
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from shared.config_loader import PipelineConfig
from shared.io import read_json, write_json
from .attempt_state import last_attempt, reserve_attempt
from shared.llm_client import OpenAICompatibleChatClient
from shared.maps_client import GoogleMapsClient
from task_module._shared.base_generator import make_folder_name
from task_module.task_schema import BenchmarkTask, WorldSliceSummary
from task_module.task_types.region_aware.generator import (
    _character_public_starting_place, _character_route_endpoint, _region_district_names,
    _stage3_route_index, _stage3_route_for,
    _stage3_price_fact, _region_opening_intervals, _region_weekday_index, _stage3_map_details,
)
from task_module.task_types.route_planning.route_cache import RouteCache
from task_module.task_types.route_planning.sampler import RouteSampler
from task_module.task_types.safety_planning.choice_artifacts import build_choice_artifacts, write_choice_artifacts
from task_module.task_types.safety_planning.choice_pipeline import construct_choice
from task_module.task_types.safety_planning.construction_agents import SafetyConstructionAgents
from task_module.task_types.safety_planning.local_candidates import local_activity
# local_activity delegates to the frozen-field match_activity implementation.
from task_module.task_types.safety_planning.evidence_index import SafetyEvidenceIndex
from task_module.task_types.safety_planning.geographic_evidence import geographic_evidence
from task_module.task_types.safety_planning.map_renderer import SafetyMapRenderer
from task_module.task_types.safety_planning.ordinary_constraints import ordinary_constraint_atoms
from task_module.task_types.safety_planning.option_selection import select_option
from task_module.task_types.safety_planning.joint_construction import construct_pair, pair_context
from task_module.task_types.safety_planning.route_exposure import route_evidence_input
from task_module.task_types.safety_planning.sampler import SafetySampler
from task_module.task_types.safety_planning.time_planning import SafetyTimeAllocator
from task_module.task_types.safety_planning.calendar_data import load_calendar, load_policy
from task_module.task_types.safety_planning.protection import ProtectionConflict
from task_module.task_types.safety_planning.history_store import (
    OCCUPANCY_HISTORY_VERSION, bind_occupancy_batch, load_generation_history, save_generation_history,
)

from task_module.task_types.safety_planning.occupancy import CONTRACT, conflicts, scope_geometry
from task_module.task_types.safety_planning.spatial_selection import SpatialOptionsExhausted
from task_module.task_types.safety_planning.retry_state import OccupancyRetries
from .opening_hours import open_at
from .task_time import time_fields
from .time_coverage import OpeningCoverage
from .time_planning import local_event_audit
from .construction_errors import ConstructionRejected
from .view_sync import sync_committed_views

GENERATION_ALGORITHM_VERSION = OCCUPANCY_HISTORY_VERSION


def _event_input(world, ordinary, identities, constraints, activity_intent, slot, character, entries,
                 for_constraint_selection=False):
    places = {p["place_id"]: p for p in world["places"]}
    geography = geographic_evidence(world, entries)
    source_places = {p["place_id"]: p for p in geography["places"]}
    districts = {d["district_id"]: d for d in geography["districts"]}
    mode, = {c["transportation_mode"] for c in constraints if c.get("transportation_mode")} or {None}
    anchors, profiles, locations, selected_places = [], [], [], []
    for ref, pid in identities.items():
        place, source = places[pid], source_places[pid]
        district = districts.get(place.get("district_id"))
        facts = {k: copy.deepcopy(place[k]) for k in ("primary_type", "primary_service_type", "service_types") if k in place}
        details = (place.get("map_projection") or {}).get("google_place_details") or {}
        facts.update({k: details[k] for k in ("outdoor_seating", "dine_in", "takeout", "delivery") if k in details})
        if district:
            facts["district_name"] = district["name"]
            if not any(a["scope_ref"] == district["source_ref"] for a in anchors):
                anchors.append({"scope_ref": district["source_ref"], "source_refs": [district["source_ref"]],
                                "public_scope": {"name": district["name"], "city_name": world["cities"][0]["name"]},
                                "geometry": district["geometry"], "exposure_basis": "place"})
        refs = [source["source_ref"]] + ([district["source_ref"]] if district else [])
        profiles.append({"candidate_ref": ref, "facts": facts})
        locations.append({"candidate_ref": ref, "source_refs": refs, "location": source["location"],
                          "district_name": district["name"] if district else None})
        selected_places.append({**source, "name": ref})
        anchors.append({"scope_ref": source["source_ref"], "source_refs": [source["source_ref"]],
                        "public_scope": {"name": place.get("name", ref), "city_name": world["cities"][0]["name"]},
                        "geometry": {"type": "Point", "coordinates": [source["location"]["lng"], source["location"]["lat"]]},
                        "exposure_basis": "place"})
    routes = []
    modes = ("WALK", "TRANSIT", "DRIVE") if for_constraint_selection else ([mode] if mode else [])
    for route_mode in modes:
        candidate_routes = {}
        origin_id = _character_route_endpoint(character)["place_id"]
        for ref, pid in identities.items():
            row = next((r for r in geography["routes"] if r["origin_place_id"] == origin_id and r["dest_place_id"] == pid and r["mode"] == route_mode), None)
            if row is None:
                if for_constraint_selection:
                    continue
                raise ValueError("Ordinary route candidate lacks frozen evidence")
            candidate_routes[ref] = row["source_ref"]
        mode_routes = route_evidence_input(geography, candidate_routes)
        routes.extend(mode_routes)
        for route in mode_routes:
            # Complete routes are GT protection dependencies, not event segments.
            anchors.append({"scope_ref": route["source_ref"], "source_refs": [route["source_ref"]],
                            "public_scope": {"city_name": world["cities"][0]["name"]},
                            "exposure_basis": "route"})
            for step in route["steps"]:
                if step["facts"].get("instruction"):
                    anchors.append({"scope_ref": step["source_ref"], "source_refs": [step["source_ref"]],
                                    "public_scope": {"city_name": world["cities"][0]["name"]},
                                    "navigation": step["facts"], "exposure_basis": "route"})
    result = {"activity_intent": activity_intent, "public_place_constraints": constraints,
            **time_fields(slot), "geographic_anchors": anchors,
            "candidate_profiles": profiles, "place_evidence": locations, "places": selected_places,
            "route_evidence": routes, "constrained_modes": [mode] if mode else [],
            "construction_context": {"fixed_target_ref": next(ref for ref, pid in identities.items() if pid == ordinary["target_place_id"]),
                                     "requirement": "Preserve the fixed target; C plus this event must exclude every ordinary competitor."}}
    # Only source-backed shapes may be chosen as event scopes; district facts
    # remain available even when a district has only a representative point.
    supported = []
    for anchor in anchors:
        try:
            scope_geometry(anchor, result)
        except ValueError:
            continue
        supported.append(anchor)
    result["geographic_anchors"] = supported
    return result


def _prepare_target(generator, target, character, cache, maps, run_dir, slot_id,
                    acquisition_id, route_acquisitions, save_audit, task_time=None):
    activity = local_activity(places=generator.world["places"], services=generator.world["services"],
                              target_id=target["place_id"], selected_service_type=target["service_type"])
    places = [p for p in generator.world["places"] if p["place_id"] in activity["local_place_ids"]]
    all_identities = activity["identities"]
    activity_ids = activity["activity_place_ids"]
    if task_time is not None:
        activity["opening_at_task_time"] = {p["place_id"]: open_at(p, task_time) for p in places}
        activity["task_time"] = task_time
        activity["open_activity_place_ids"] = [pid for pid in activity_ids
            if activity["opening_at_task_time"][pid] is True]
        activity_ids = activity["open_activity_place_ids"]
    if set(activity.get("universe_place_ids", ())) < set(activity_ids):
        raise ValueError("Local Safety pool is not contained in the service-match universe")
    if target["place_id"] not in activity_ids or target["place_id"] not in activity["local_place_ids"]:
        raise ValueError("Fixed Safety target is absent from U or L")
    write_json(run_dir / f"{slot_id}_{acquisition_id.split(':')[-1]}_activity_matching.json", activity)
    if len(activity_ids) < 2:
        raise ValueError("Activity has no ordinary competitor")
    before = cache.stats_snapshot()
    origin = _character_route_endpoint(character)
    route_queries = []
    relevant_entries = []
    for place in places:
        if place["place_id"] not in activity_ids:
            continue
        for mode in RouteSampler._modes_for_distance(RouteSampler.distance(origin, place)):
            entry = (cache.get_or_compute(origin, place, mode, maps) if maps else cache.get(origin, place, mode))
            route_queries.append({"from_place_id": origin["place_id"], "to_place_id": place["place_id"], "mode": mode,
                                  "route_id": (entry or {}).get("route_id"), "available": ((entry or {}).get("route") or {}).get("available")})
            if entry is not None:
                relevant_entries.append(cache._resolved_entry(entry))
    route_acquisitions.append({"acquisition_id": acquisition_id, "route_queries": route_queries,
        "route_cost_delta": {k: v - (before.get(k) or 0) for k, v in cache.stats_snapshot().items() if isinstance(v, (int, float))}})
    save_audit()
    route_snapshot = {"cache": relevant_entries}
    route_index = _stage3_route_index(route_snapshot)
    activity_identities = {ref: pid for ref, pid in all_identities.items() if pid in activity_ids}
    district_names = _region_district_names(generator.world)
    query_names = sorted({district_names[p["district_id"]] for p in places
                          if p.get("district_id") in district_names})
    if not query_names:
        query_names = [generator.world["cities"][0]["name"]]
    return (activity, places, all_identities, activity_ids, relevant_entries, route_snapshot, route_index, activity_identities, query_names)


def generate_choices(generator, count):
    from task_module.task_types.safety_planning.generator import _resolve_safety_routing_place_ids
    assets = generator.safety_assets
    if not assets or not generator.use_llm_prompt:
        raise ValueError("Formal Safety requires version-bound inputs and a configured construction model; legacy generation is not a fallback")
    if count < 1 or generator.max_attempts_per_slot < 1:
        raise ValueError("Safety count and attempt limit must be positive")
    output = generator.output_dir
    output.mkdir(parents=True, exist_ok=True)
    batch_binding = bind_occupancy_batch(output, world_binding=assets["world_binding"])
    path = Path(assets["route_cache"])
    cache = RouteCache(generator.city_id, output_root=str(path.parents[2]), cache_namespace=path.parent.name)
    if cache.cache_path.resolve() != path.resolve():
        raise ValueError("Safety route cache does not match the bound city-version path")
    pool = cache.cache_dir / "route_poi_pool.json"
    routing = _resolve_safety_routing_place_ids(generator.world, read_json(pool) if pool.is_file() else {}, list(cache._cache.values()))
    if not routing:
        raise ValueError("Safety requires current-version Routing POI coverage")
    config = PipelineConfig(generator.config_yaml)
    calendar = load_calendar(generator.city_id)
    policy = load_policy(generator.city_id, point_time=True)
    if calendar["timezone"] != generator.world["cities"][0]["timezone"]:
        raise ValueError("Calendar timezone differs from the frozen city")
    allocator = SafetyTimeAllocator(calendar=calendar, policy=policy,
        task_anchors=(generator.safety_time_sources or {}).get("task_anchors", []),
        world_binding=assets["world_binding"], batch_seed=generator.seed)
    endpoint_name = config.stage_model(generator.stage_model_key)
    endpoint = config.model_endpoint(endpoint_name)
    client = OpenAICompatibleChatClient(endpoint["base_url"], endpoint["api_key"], endpoint["model"],
                                        endpoint_name=endpoint_name, pricing_reference=endpoint.get("pricing_reference"))
    agents = SafetyConstructionAgents(client)
    maps = None if generator.route_cache_only else GoogleMapsClient(config)
    index = load_generation_history(output, world_binding=assets["world_binding"],
                                    algorithm_version=GENERATION_ALGORITHM_VERSION)
    sync_committed_views(output, index)
    existing_indices = []
    for p in (output / "tasks").glob("task_*"):
        try:
            existing_indices.append(int(p.name.split("_")[1]))
        except (ValueError, IndexError):
            continue
    next_index = max(existing_indices, default=0) + 1
    slot_ids = [f"slot_{i}" for i in range(next_index, next_index + count)]
    started = time.monotonic()
    tasks = []
    attempts_path = output / "candidate_attempts.json"
    attempts = read_json(attempts_path).get("attempts", []) if attempts_path.is_file() else []
    messages_path = output / "construction_messages.json"
    prior_calls = read_json(messages_path) if messages_path.is_file() else []
    invocation = 1
    while (output / "generation_runs" / f"run_{invocation:04d}").exists():
        invocation += 1
    run_dir = output / "generation_runs" / f"run_{invocation:04d}"
    run_dir.mkdir(parents=True)
    route_acquisitions = []
    time_audit_path = output / "safety_time_allocations.json"
    previous_time_runs = read_json(time_audit_path).get("runs", []) if time_audit_path.is_file() else []

    def save_audit():
        write_json(messages_path, prior_calls + agents.calls)
        write_json(attempts_path, {"attempts": attempts})
        write_json(run_dir / "route_acquisitions.json", route_acquisitions)
        write_json(run_dir / "construction_usage.json", client.telemetry_snapshot())
        write_json(run_dir / "safety_time_plan.json", allocator.snapshot())
        write_json(time_audit_path, {"schema_version": "safety_time_runs.v1", "runs": previous_time_runs + [
            {"run_id": run_dir.name, "plan": allocator.snapshot()}]})

    agents.on_audit_update = save_audit
    coverage = OpeningCoverage(generator.world)
    local_cache = {}
    districts = _region_district_names(generator.world)

    def target_region(target):
        key = (target["place_id"], target["service_type"])
        if key not in local_cache:
            local = local_activity(places=generator.world["places"], services=generator.world["services"],
                target_id=key[0], selected_service_type=key[1])
            names = sorted({p.get("district_id") for p in generator.world["places"]
                if p["place_id"] in local["local_place_ids"] and p.get("district_id") in districts})
            local_cache[key] = local, [districts[name] for name in names] or [generator.world["cities"][0]["name"]]
        return local_cache[key]

    try:
        for offset, slot_id in enumerate(slot_ids):
            slot_call_start = len(agents.calls)
            number = next_index + offset
            rng = generator.activate_task_attempt(number, 1)
            generator.set_cost_unit(f"slot_{number:04d}")
            sampler = SafetySampler(generator.world, generator.characters, cache, rng=rng, routing_place_ids=routing)
            sampled_character = sampler.sample_character()
            if not sampled_character:
                raise ValueError("Safety cannot sample its fixed character and target")
            character = next(c for c in generator.characters["characters"] if c["character_id"] == sampled_character["character_id"])
            starting = _character_public_starting_place(character, generator.world, _region_district_names(generator.world))
            failures = []
            retries = OccupancyRetries()
            seen_targets = set()
            need_target = True
            slot = None
            accepted = False
            attempt = last_attempt(output, number, attempts)
            event_attempts = 0
            while event_attempts < generator.max_attempts_per_slot:
                attempt += 1
                rng = generator.activate_task_attempt(number, attempt)
                reserve_attempt(output, generator._active_seed_lineage)
                sampler = SafetySampler(generator.world, generator.characters, cache, rng=rng, routing_place_ids=routing)

                def allowed_targets(stamp, cluster_id):
                    opened = set(coverage.at(stamp)["place_ids"])
                    eligible = []
                    audit = []
                    for candidate in sampler.target_candidates(task_time=stamp):
                        local, names = target_region(candidate)
                        if len(opened.intersection(local["activity_place_ids"])) < 2:
                            audit.append({"target_place_id": candidate["place_id"], "reason": "no_open_local_competitor"})
                            continue
                        if cluster_id in retries.exclusions(names):
                            audit.append({"target_place_id": candidate["place_id"], "query_names": names,
                                          "reason": "regional_occupancy_exhausted"})
                            continue
                        if slot is not None and stamp == slot["task_time"] and candidate["place_id"] in seen_targets:
                            continue
                        eligible.append(candidate["place_id"])
                    return {"eligible_target_ids": eligible, "audit": audit}

                if slot is None:
                    slot = allocator.choose_point(slot_id=slot_id, index=index, coverage=coverage,
                                                  allowed_targets=allowed_targets,
                                                  selection_seed=generator._active_seed_lineage.get("attempt_seed"))
                if need_target:
                    eligible = set(allowed_targets(slot["task_time"], slot["cluster_id"])["eligible_target_ids"])
                    target = sampler.sample_risky_target(
                        excluded_place_ids=set(sampler.place_dict) - eligible, task_time=slot["task_time"])
                    if not target:
                        blocked_regions = [region for (region, key), tried in retries.blocked_targets.items()
                                           if key == slot["cluster_id"] and tried]
                        if not blocked_regions:
                            break
                        for region in blocked_regions:
                            retries.exclusions(region).add(slot["cluster_id"])
                        allocator.record(slot, status="rejected", reason="spatial_target_resources_exhausted")
                        seen_targets.clear()
                        slot = None
                        continue
                seen_targets.add(target["place_id"])
                acquisition_id = f"run_{invocation:04d}:{slot_id}:routes_{attempt}"
                (activity, places, all_identities, activity_ids, relevant_entries, route_snapshot, route_index, activity_identities, query_names) = _prepare_target(
                    generator, target, character, cache, maps, run_dir, slot_id,
                    acquisition_id, route_acquisitions, save_audit, task_time=slot["task_time"])
                slot["query_names"] = query_names
                slot["visible_event_count"] = None
                slot.pop("event_count_audit", None)
                event_started = False
                time_context = {"instruction_time": slot["task_time"],
                                "episode_start": slot["task_time"], "timezone": calendar["timezone"],
                                "source": "safety_calendar_point_assignment.v2"}
                task_context = {"traveler": {"name": character["name"], "occupation": character.get("occupation", "")},
                                "city": generator.world["cities"][0]["name"], "starting_place": starting,
                                "date_time": time_context, "task_time": slot["task_time"]}
                # Time-dependent facts are recomputed when a protected cluster is skipped.
                atoms = ordinary_constraint_atoms(target_id=target["place_id"], places=places, activity_place_ids=activity_ids,
                    character=character, route_index=route_index, world=generator.world, time_context=time_context)
                candidate_context = _event_input(generator.world, {"target_place_id": target["place_id"]}, activity_identities,
                    [], activity["activity_intent"], slot, character, relevant_entries, for_constraint_selection=True)
                candidate_context["time_allocation"] = copy.deepcopy(slot)
                kinds = {a["constraint"]["type"] for a in atoms}
                features = {a["constraint"]["value"] for a in atoms if a["constraint"]["type"] == "service_feature"}
                by_id = {p["place_id"]: p for p in places}
                for profile in candidate_context["candidate_profiles"]:
                    place = by_id[activity_identities[profile["candidate_ref"]]]
                    facts = profile["facts"]
                    details = _stage3_map_details(place)
                    facts.update({f: details[f] for f in features if details.get(f) is not None})
                    if kinds & {"price_maximum", "price_target_amount"}:
                        low, high, currency = _stage3_price_fact(place)
                        if low is not None and high is not None and currency:
                            facts["price_range"] = {"minimum": str(low), "maximum": str(high), "currency": currency}
                    if kinds & {"opens_by", "closes_at_or_after"}:
                        intervals = _region_opening_intervals(place, _region_weekday_index(time_context))
                        if intervals:
                            facts["opening_intervals_minutes"] = intervals
                owner = f"task_{generator.city_id}_safety_planning_{number:04d}"
                record = {"slot": slot_id, "target_place_id": target["place_id"], "seed_lineage": dict(generator._active_seed_lineage),
                          "route_acquisition_id": acquisition_id, "status": "pending",
                          "time_allocation": copy.deepcopy(slot),
                          "excluded_from_upper_bound_if_rejected": True}
                attempt_started = time.monotonic()
                record["started_at"] = datetime.now(timezone.utc).isoformat()
                agents.audit_context = {"algorithm_version": GENERATION_ALGORITHM_VERSION, "batch_id": output.name,
                                        "run_id": run_dir.name, "slot": slot_id, "attempt": attempt,
                                        "seed_lineage": dict(generator._active_seed_lineage)}
                attempts.append(record)
                staged = SafetyEvidenceIndex.from_dict(index.to_dict(), world_binding=assets["world_binding"])
                try:
                    def check_option(proposal):
                        selected_ids = {ref: pid for ref, pid in activity_identities.items() if pid in proposal["ordinary_place_ids"]}
                        ctx = _event_input(generator.world, proposal, selected_ids, proposal["constraints"],
                            activity["activity_intent"], slot, character, relevant_entries)
                        ctx["protection_contract"] = CONTRACT
                        ctx = staged.prepare(ctx, event_generation_window=slot["event_generation_window"])
                        return conflicts(ctx)

                    record["c_selection"] = {}
                    # A target location is indispensable regardless of which C is selected.
                    fixed_ctx = _event_input(generator.world, {"target_place_id": target["place_id"]},
                        {ref: pid for ref, pid in activity_identities.items() if pid == target["place_id"]},
                        [], activity["activity_intent"], slot, character, relevant_entries)
                    fixed_ctx["protection_contract"] = CONTRACT
                    fixed_conflicts = conflicts(staged.prepare(fixed_ctx, event_generation_window=slot["event_generation_window"]))
                    if fixed_conflicts:
                        record["required_gt_overlap"] = fixed_conflicts
                        raise SpatialOptionsExhausted({"fixed_gt": fixed_conflicts})
                    option = select_option(agents, atoms=atoms, identities=activity_identities,
                        target_id=target["place_id"], context=candidate_context,
                        city=generator.world["cities"][0]["name"], rng=rng, audit=record["c_selection"],
                        overlap_check=check_option)
                    identities = {ref: pid for ref, pid in activity_identities.items() if pid in option["ordinary_place_ids"]}
                    candidate_context = _event_input(generator.world, option, identities,
                        option["constraints"], activity["activity_intent"], slot, character, relevant_entries)
                    slot["event_count_audit"] = local_event_audit(index, candidate_context)
                    slot["visible_event_count"] = slot["event_count_audit"]["count"]
                    record["time_allocation"] = copy.deepcopy(slot)
                    candidate_context["time_allocation"] = copy.deepcopy(slot)
                    candidate_context["protection_contract"] = CONTRACT
                    record.update(constraints=option["constraints"], ordinary_place_ids=option["ordinary_place_ids"],
                                  selection_reason=option["selection_reason"], pairing_review=option["pairing_review"])
                    save_audit()
                    event_attempts += 1
                    event_started = True
                    need_target = False
                    option, authored_event = construct_pair(agents, option=option, identities=identities,
                        target_id=target["place_id"], context=candidate_context, evidence_index=staged,
                        event_generation_window=slot["event_generation_window"],
                        map_renderer=SafetyMapRenderer(output / "private_maps" / slot_id), failures=failures)
                    ordinary_ids = option["ordinary_place_ids"]
                    identities = {ref: pid for ref, pid in all_identities.items() if pid in ordinary_ids}
                    record.update(constraints=option["constraints"], ordinary_place_ids=ordinary_ids,
                                  selection_reason=option["selection_reason"], event=authored_event,
                                  pairing_review=option.get("pairing_review"))
                    event_input = pair_context(candidate_context, option, authored_event, activity_identities,
                        event_generation_window=slot["event_generation_window"])
                    route_minutes = {}
                    for ref, pid in identities.items():
                        route_minutes[ref] = {}
                        place = next(p for p in places if p["place_id"] == pid)
                        for mode in event_input["constrained_modes"]:
                            raw = _stage3_route_for(route_index, character, place, mode)["route"]["duration_s"]
                            route_minutes[ref][mode] = max(1, math.ceil(raw / 60))
                    writing = {"task_context": task_context, "activity_intent": activity["activity_intent"],
                               "public_place_constraints": option["constraints"],
                               "response_requirement": "Return the selected place's name and location, the relevant safety information you obtained, and explain your choice."}
                    choice = construct_choice(agents=agents, event_input=event_input, writer_input=writing,
                        target_ref=next(ref for ref, pid in identities.items() if pid == target["place_id"]), ordinary_refs=list(identities),
                        scope_members={}, route_minutes=route_minutes, evidence_index=staged, owner=owner,
                        event_generation_window=slot["event_generation_window"], authored_event=authored_event)
                    choice["model_calls"] = copy.deepcopy(agents.calls[slot_call_start:])
                    artifacts = build_choice_artifacts(task_id=owner, city_id=generator.city_id, generated_at=generator.generated_at,
                        time_context=time_context, choice=choice, evidence_index=staged, identities=identities,
                        world=generator.world, character=character, activity_place_ids=activity["activity_place_ids"], route_cache=route_snapshot,
                        metadata={"algorithm_version": GENERATION_ALGORITHM_VERSION, "batch_binding": batch_binding,
                                  "seed_lineage": dict(generator._active_seed_lineage), "model": client.model, "endpoint_name": endpoint_name,
                                  "safety_time_binding": {"cluster_id": slot["cluster_id"],
                                      "calendar_sha256": calendar["calendar_sha256"], "policy_sha256": policy["sha256"]}})
                    artifacts["generation_log/activity_matching.json"] = activity
                    artifacts["generation_log/pairing_selection.json"] = copy.deepcopy(option)
                    artifacts["generation_log/c_selection.json"] = copy.deepcopy(record["c_selection"])
                    artifacts["generation_log/time_allocation.json"] = {
                        "allocation": copy.deepcopy(slot), "calendar": calendar, "policy": policy}
                    folder = output / "tasks" / make_folder_name(number, choice["user_prompt"])
                    write_choice_artifacts(folder, artifacts)
                    report = generator.finalize_task_folder(folder)
                    if not report["valid"]:
                        raise ValueError(f"Safety formalization: {report['errors']}")
                    save_generation_history(output, staged, algorithm_version=GENERATION_ALGORITHM_VERSION)
                    index = staged
                    record.update(status="accepted", task_dir=str(folder))
                    save_audit()
                    sync_committed_views(output, index)
                    task = artifacts["task.json"]
                    tasks.append(BenchmarkTask(task_id=owner, task_type="safety_planning", city_id=generator.city_id,
                        difficulty="medium", generated_at=generator.generated_at, user_prompt=task["user_prompt"],
                        time_context=time_context, summary=task["summary"], metadata=task["metadata"],
                        world_slice_summary=WorldSliceSummary(places=artifacts["world_slice.json"]["places"])))
                    record.update(status="accepted", task_dir=str(folder))
                    allocator.record(slot, status="accepted")
                    accepted = True
                    break
                except (ConstructionRejected, SpatialOptionsExhausted, ProtectionConflict) as exc:
                    record.update(status="rejected", reason=str(exc), error_class=type(exc).__name__)
                    spatial = isinstance(exc, SpatialOptionsExhausted)
                    kind = "preflight_occupancy_overlap" if spatial else ("event_occupancy_conflict" if isinstance(exc, ProtectionConflict) else "ordinary_construction_failure")
                    record["failure_kind"] = kind
                    allocator.record(slot, status="rejected", reason=kind)
                    if spatial:
                        record["spatial_conflicts"] = exc.conflicts
                        action = retries.reject(region=query_names, cluster_id=slot["cluster_id"], target_id=target["place_id"])
                        record["backtrack"] = action
                        need_target = action == "next_gt"
                        if action == "next_time_cluster":
                            seen_targets.clear()
                            slot = None
                            need_target = True
                    elif not event_started:
                        event_attempts += 1
                    if isinstance(exc, ProtectionConflict):
                        record["blocking_record_refs"] = exc.record_refs
                        record["conflict_details"] = copy.deepcopy(getattr(exc, "details", []))
                    failures.append({"constraints": record.get("constraints"), "event": record.get("event"),
                                     "reason": str(exc), "failure_kind": kind,
                                     "cluster_id": record["time_allocation"]["cluster_id"],
                                     "conflict_details": copy.deepcopy(getattr(exc, "details", []))})
                except BaseException as exc:
                    reason = str(exc)
                    secret = getattr(client, "api_key", None)
                    if isinstance(secret, str) and secret:
                        reason = reason.replace(secret, "[REDACTED]")
                    committed = any(entry["owner"] == owner for entry in index.entries)
                    record.update(status="accepted_sync_failed" if committed else "aborted", reason=reason,
                                  error_class=type(exc).__name__,
                                  failure_kind="interrupted" if not isinstance(exc, Exception) else
                                  ("contract_failure" if isinstance(exc, (ValueError, KeyError, TypeError)) else "runtime_failure"))
                    raise
                finally:
                    record["finished_at"] = datetime.now(timezone.utc).isoformat()
                    record["elapsed_seconds"] = round(time.monotonic() - attempt_started, 6)
                    save_audit()
            if not accepted:
                # Exhausting the event/C-combination layer returns to the
                # existing occupancy retry ladder: first another GT, then a
                # new time cluster. Do not terminate the whole batch.
                action = retries.reject(region=query_names, cluster_id=slot["cluster_id"], target_id=target["place_id"])
                attempts[-1]["backtrack"] = action
                if action == "next_time_cluster":
                    seen_targets.clear()
                    slot = None
                    need_target = True
                else:
                    need_target = True
                event_attempts = 0
                continue
        # Publish the same aggregate notice view for the newly built tasks.
        # Their GT and private event proofs remain unchanged.
        sync_committed_views(output, index)
        return tasks
    finally:
        save_audit()
        write_json(output / "construction_usage.json", client.telemetry_snapshot())
        cache.save()
        from task_module._shared.generation_telemetry import write_generation_telemetry
        write_generation_telemetry(output, task_type="safety_planning", city_id=generator.city_id, requested_count=count,
            generated_count=len(tasks), elapsed_s=time.monotonic() - started, route_cache=cache,
            llm_usage=client.telemetry_snapshot()["token_usage"], skipped_count=count - len(tasks))
