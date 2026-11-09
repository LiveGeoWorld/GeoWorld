"""Target-first Discovery construction. Scripts own C; writer chooses and narrates."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shared.config_loader import PipelineConfig
from shared.io import write_json
from task_module._shared.discovery_fact_helpers import (
    _google_details, _price_range, _service_features, _weekly_opening_intervals,
)
from task_module._shared.discovery_fact_helpers import (
    _natural_candidate_constraints, _matches, _route_constraint_options,
)
from .constraint_combinations import rank_five_pool, build_options
from .sampler import SampledDiscoveryTarget, _character_start_context, _factual_place_aliases
from .llm_writer import _chat_completion, _message_content, parse_llm_json_response


def sample_target(world, characters, rng, excluded):
    people = characters.get("characters") or []
    if not people:
        raise ValueError("Discovery needs a frozen character")
    character = rng.choice(people)
    start = _character_start_context(world, character, (world.get("cities") or [{}])[0].get("city_id", ""))
    if not start:
        raise ValueError("Selected character lacks an identifiable public origin")
    services = world.get("services") or _factual_place_aliases(world)
    by_place = {}
    for service in services:
        by_place.setdefault(service.get("place_id"), service)
    for alias in _factual_place_aliases(world):
        by_place.setdefault(alias["place_id"], alias)
    places = [p for p in world.get("places", []) if p.get("place_id") in by_place
              and by_place[p["place_id"]].get("service_id") not in excluded]
    if not places:
        raise ValueError("No remaining frozen GT")
    gt = rng.choice(places)
    if gt.get("place_id") == start.get("place_id") or gt.get("location") == start.get("coordinate"):
        raise ValueError("Public starting place cannot be the fixed GT")
    primary = gt.get("primary_type")
    if not primary:
        raise ValueError("Selected GT has no public type")
    # Any filtering attribute is explicitly retained as base C.
    pool = rank_five_pool(gt, [p for p in world.get("places", [])
                              if p.get("primary_type") == primary])
    if len(pool["places"]) < 3:
        raise ValueError("Discovery representative top-3 requires two competitors")
    target = SampledDiscoveryTarget(
        city_id=(world.get("cities") or [{}])[0].get("city_id", ""),
        service_type=str(by_place[gt["place_id"]].get("primary_service_type")
                         or by_place[gt["place_id"]].get("service_type") or primary),
        gold_service=by_place[gt["place_id"]], gold_place=gt, start=start,
        candidate_services=[by_place[p["place_id"]] for p in pool["places"][:3]],
        search_landscape={}, character=character, character_constraints=None,
    )
    return target, pool


def project_places(world, pool):
    districts = {d["district_id"]: d.get("name", "") for d in world.get("districts", [])}
    result = []
    for p in pool["places"]:
        details = _google_details(p)
        result.append({**p, "district_name": districts.get(p.get("district_id"), ""),
                       "price_range": _price_range(details),
                       "service_features": _service_features(details),
                       "weekly_opening_intervals": _weekly_opening_intervals(details)})
    return result


def compute_contract(world, target, pool, cache, maps_client, rng):
    places = project_places(world, pool)
    gt = places[0]
    atoms = []
    base = [{"type": "primary_type", "operator": "equals", "value": gt["primary_type"]}]
    # Enumerate actual weekday facts, not an invented task date. Each opening
    # atom carries its weekday; the writer must express that weekday.
    for day in range(7):
        for c in _natural_candidate_constraints(gt, places, day):
            if c["type"] == "primary_type":
                continue
            c = dict(c)
            if c["type"] in {"opens_by", "closes_at_or_after"}:
                c["weekday_index"] = day
            matches = {}
            for p in places:
                kind = c["type"]
                known = bool(p.get(kind))
                if kind.startswith("price_"):
                    known = bool(p.get("price_range"))
                elif kind == "service_feature":
                    flag = _google_details(p).get(c["value"])
                    known = type(flag) is bool
                elif kind in {"opens_by", "closes_at_or_after"}:
                    known = bool(p["weekly_opening_intervals"].get(str(day)))
                matches[p["place_id"]] = _matches(p, [c], day) if known else False
            atoms.append({"constraint_id": f"atom_{len(atoms):04d}", "constraint": c,
                          "source": {"field": c["type"], "frozen_place_ids": [p["place_id"] for p in places]},
                          "matches": matches})

    start = target.start
    origin = {"place_id": target.character["character_id"], "name": start.get("place_name") or start.get("name"),
              "location": start.get("location") or start.get("coordinate")
              or {"lat": start.get("lat"), "lng": start.get("lng")}}
    routes = []
    for mode in ("WALK", "TRANSIT", "DRIVE"):
        for p in places:
            entry = cache.get_or_compute(origin, p, mode, maps_client)
            if entry.get("api_error") or entry.get("error") == "API_FAILED":
                raise RuntimeError(f"Navigation infrastructure failure: {mode} {p['place_id']}")
            routes.append(entry)
        timeline = [{"block_kind": "transit", "from_place_id": origin["place_id"], "mode": mode},
                    {"block_kind": "poi_activity"}]
        for option in _route_constraint_options(gt, places, timeline, 1, cache, {origin["place_id"]: origin}):
            c = dict(option["constraint"])
            # Private time-repair hints are not writer-authored facts. Preserve
            # the already computed bounds and source values for narration.
            c.pop("_time_repair", None)
            matches = {}
            for p in places:
                route = (cache.get(origin, p, mode) or {}).get("route", {})
                seconds = route.get("duration_s")
                matches[p["place_id"]] = (False if route.get("available") is False else
                    c["minimum"] <= seconds / 60 <= c["maximum"]
                    if isinstance(seconds, (int, float)) and route.get("available") is not False else False)
            atoms.append({"constraint_id": f"atom_{len(atoms):04d}", "constraint": c,
                          "source": {"route_ids": [e.get("route_id") for e in routes if e.get("mode") == mode]},
                          "matches": matches})
        # Reuse Region's source-backed scalar boundary derivation for walking
        # distance (the whole WALK route, or walking legs of TRANSIT/DRIVE).
        from task_module._shared.discovery_fact_helpers import _region_scalar_range_constraint
        metric = "distance_m" if mode == "WALK" else "walking_distance_m"
        values = {}
        unreachable = set()
        for p in places:
            route = (cache.get(origin, p, mode) or {}).get("route", {})
            if route.get("available") is False:
                unreachable.add(p["place_id"])
            elif isinstance(route.get(metric), (int, float)):
                values[p["place_id"]] = int(route[metric])
        if values.get(gt["place_id"], 0) > 0:
            c = _region_scalar_range_constraint(constraint_id=f"walking_distance_{mode.lower()}",
                kind="walking_distance_range", target_value=values[gt["place_id"]],
                other_values=[v for pid, v in values.items() if pid != gt["place_id"]],
                unit="meter", mode=mode)
            c["mode"] = mode
            atoms.append({"constraint_id": f"atom_{len(atoms):04d}", "constraint": c,
                "source": {"route_ids": [e.get("route_id") for e in routes if e.get("mode") == mode]},
                "matches": {p["place_id"]: False if p["place_id"] in unreachable else
                    c["minimum"] <= values[p["place_id"]] <= c["maximum"]
                    if p["place_id"] in values else False for p in places}})
    cache.save()
    contract = build_options(target_id=gt["place_id"], candidate_ids=[p["place_id"] for p in places],
                             base_constraints=base, atoms=atoms, rng=rng)
    contract.update(schema_version="discovery.factual_C.v2", pool=pool, route_evidence=routes,
                    public_origin=origin, candidate_facts=places)
    return contract


def write_selected_prompt(*, contract, target, config, audit_path):
    endpoint = getattr(config, "writer_endpoint_override", None)
    if endpoint is None:
        cfg = PipelineConfig(config.config_yaml)
        key = cfg.stage_model(config.prompt_model_stage)
        endpoint = cfg.model_endpoint(key)
    else:
        key = "local_proxy_preview"
    instructions = Path(__file__).with_name("prompts").joinpath("factual_system.md").read_text(encoding="utf-8")
    def public_constraint(c):
        c = dict(c)
        if "weekday_index" in c:
            c["weekday"] = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")[c["weekday_index"]]
        return c
    options = [{"option_id": o["option_id"],
                "constraints": [public_constraint(c) for c in [*o["base_constraints"], *o["constraints"]]]}
               for o in contract["options"]]
    request = {"prompt_language": "en",
               "city": target.start.get("city_name") or target.city_id,
               "public_origin": {"name": contract["public_origin"]["name"], "address": target.start.get("address")},
               "character": {"name": target.character.get("name"), "occupation": target.character.get("occupation")},
               "options": options}
    messages = [{"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)}]
    audit = {"endpoint": key, "request": request, "attempts": []}
    allowed = {o["option_id"]: o for o in contract["options"]}
    for attempt in range(3):
        try:
            raw = _chat_completion(endpoint, messages, config.prompt_temperature)
        except Exception as exc:
            audit["attempts"].append({"messages": list(messages), "error": str(exc),
                                      "classification": "infrastructure_failure"})
            write_json(audit_path, audit)
            raise
        content = _message_content(raw)
        errors = []
        output = {}
        try:
            output = parse_llm_json_response(content)
            if output.get("option_id") not in allowed:
                errors.append("option_id must identify one supplied option")
            if not isinstance(output.get("user_prompt"), str) or not output["user_prompt"].strip():
                errors.append("user_prompt must be nonempty")
            option = allowed.get(output.get("option_id"))
            spans = output.get("constraint_spans")
            if option:
                count = len(option["base_constraints"]) + len(option["constraints"])
                if not isinstance(spans, list) or len(spans) != count or any(
                        not isinstance(s, str) or not s or s not in output.get("user_prompt", "") for s in spans):
                    errors.append("constraint_spans must contain one exact prompt excerpt per selected condition, in input order")
            if target.gold_place.get("name", "").casefold() in output.get("user_prompt", "").casefold():
                errors.append("Do not reveal the target POI name")
        except (ValueError, TypeError) as exc:
            errors.append(str(exc))
        audit["attempts"].append({"messages": list(messages), "raw_response": raw, "output": output, "errors": errors})
        write_json(audit_path, audit)
        if not errors:
            return output, allowed[output["option_id"]], audit
        messages.extend([{"role": "assistant", "content": content},
                         {"role": "user", "content": json.dumps({"validation_errors": errors})}])
    raise ValueError("Writer repair exhausted: " + "; ".join(errors))
