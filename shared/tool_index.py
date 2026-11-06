"""Build design-time indexes over a v7 world.

The index is not a second source of truth. It is a tool-serving structure for a
ScenarioDesignerAgent that needs to search, inspect, and compose world
materials without reading the entire world JSON as one prompt blob.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


APP_SURFACES: dict[str, dict[str, Any]] = {
    "UserInterfaceApp": {
        "read": [],
        "write": ["send_message_to_agent", "send_message_to_user"],
        "observes": ["user_request", "final_response"],
    },
    "MapApp": {
        "read": ["places", "districts", "corridors", "traffic_alerts", "transit_alerts"],
        "write": ["route_query", "save_place"],
        "observes": ["place_location", "mobility_alert", "traffic", "transit"],
    },
    "LocalServiceApp": {
        "read": ["services", "queue_time", "reservation_inventory", "open_status"],
        "write": ["make_reservation", "cancel_reservation", "modify_reservation"],
        "observes": ["service_state", "slot_inventory_changed"],
    },
    "ReviewSocialApp": {
        "read": ["reviews", "posts", "stale_information"],
        "write": ["optional_save_or_share"],
        "observes": ["review", "social_post", "information_conflict"],
    },
    "WeatherCityEventApp": {
        "read": ["weather", "city_alerts"],
        "write": [],
        "observes": ["weather", "city_event"],
    },
    "ContactsApp": {
        "read": ["contact_profiles"],
        "write": ["optional_edit_contact"],
        "observes": ["actor_identity", "stable_profile_hint"],
    },
    "MessagingAppV2": {
        "read": ["conversations", "messages"],
        "write": ["send_message", "create_conversation"],
        "observes": ["late_actor_constraint", "coordination_update"],
    },
    "CalendarApp": {
        "read": ["calendar_events", "availability", "free_busy"],
        "write": ["add_event", "update_event", "delete_event"],
        "observes": ["time_constraint"],
    },
}


def build_world_tool_index(world: dict[str, Any]) -> dict[str, Any]:
    """Return design-time indexes for director tools."""

    objects = _object_index(world)
    relations = _relation_index(world, objects)
    affordances = _affordance_index(world)
    dynamic_hooks = _dynamic_hook_index(world)
    observability = _observability_index(world, dynamic_hooks)
    app_surfaces = _app_surface_index(world, observability)
    return {
        "schema_version": "v7.world_tool_index.v1",
        "purpose": "design_time_index_for_scenario_designer_agent",
        "truth_source": "world.json",
        "visibility_scope": {
            "intended_consumer": "ScenarioDesignerAgent",
            "may_expose_hidden_design_material": True,
            "must_not_be_shown_to_benchmark_agent": True,
        },
        "counts": {
            "objects": len(objects),
            "relations": len(relations["edges"]),
            "affordance_groups": sum(len(v) for v in affordances.get("by_affordance", {}).values()),
            "dynamic_hooks": len(dynamic_hooks["events"]),
        },
        "object_index": objects,
        "relation_index": relations,
        "affordance_index": affordances,
        "observability_index": observability,
        "dynamic_hook_index": dynamic_hooks,
        "app_surface_index": app_surfaces,
        "query_capabilities": [
            "overview_world",
            "search_objects",
            "inspect_object",
            "find_related",
            "find_dynamic_hooks",
            "trace_observability",
            "explain_execution_path",
        ],
    }


def _object_index(world: dict[str, Any]) -> dict[str, dict[str, Any]]:
    objects: dict[str, dict[str, Any]] = {}
    for city in world.get("cities", []):
        objects[city["city_id"]] = _summary("city", city["city_id"], city.get("name"), city)
    for district in world.get("districts", []):
        objects[district["district_id"]] = {
            **_summary("district", district["district_id"], district.get("name"), district),
            "role_tags": district.get("role_tags", []),
            "dominant_flows": district.get("dominant_flows", []),
            "search_text": _join_search_text(
                district.get("name"),
                district.get("role_tags", []),
                district.get("dominant_flows", []),
                district.get("director_notes", {}),
            ),
        }
    for corridor in world.get("corridors", []):
        objects[corridor["corridor_id"]] = {
            **_summary("corridor", corridor["corridor_id"], corridor.get("name"), corridor),
            "corridor_type": corridor.get("corridor_type"),
            "served_district_ids": corridor.get("served_district_ids") or [
                rel.get("target_id")
                for rel in corridor.get("relations", [])
                if rel.get("target_type") == "district"
            ],
            "semantic_role": corridor.get("semantic_role", ""),
            "route_runtime_ref": corridor.get("route_runtime_ref", {}),
            "is_full_routing_graph": bool((corridor.get("route_polyline") or {}).get("is_full_routing_graph")),
            "search_text": _join_search_text(
                corridor.get("name"),
                corridor.get("corridor_type"),
                corridor.get("semantic_role"),
                corridor.get("control_tags", {}),
                corridor.get("director_notes", {}),
            ),
        }
    for place in world.get("places", []):
        objects[place["place_id"]] = {
            **_summary("place", place["place_id"], place.get("name"), place),
            "primary_type": place.get("primary_type"),
            "district_id": place.get("district_id"),
            "near_corridor_ids": place.get("near_corridor_ids", []),
            "rating": place.get("map_projection", {}).get("rating"),
            "search_text": _join_search_text(
                place.get("name"),
                place.get("primary_type"),
                place.get("address"),
                place.get("map_projection", {}).get("types", []),
            ),
        }
    for service in world.get("services", []):
        objects[service["service_id"]] = {
            **_summary("service", service["service_id"], service.get("service_id"), service),
            "place_id": service.get("place_id"),
            "service_type": service.get("service_type"),
            "affordances": service.get("affordances", []),
            "apps": sorted(set(["LocalServiceApp", *[o.get("app", "") for o in service.get("oracle_affordances", []) if o.get("app")]])),
            "oracle_functions": sorted(set(o.get("function") for o in service.get("oracle_affordances", []) if o.get("function"))),
            "state_keys": sorted((service.get("state_model") or {}).keys()),
            "search_text": _join_search_text(
                service.get("service_id"),
                service.get("service_type"),
                service.get("affordances", []),
                service.get("state_model", {}),
            ),
        }
    for actor in world.get("actors", []):
        objects[actor["actor_id"]] = {
            **_summary("actor", actor["actor_id"], actor.get("name"), actor),
            "relationship": actor.get("relationship_to_user"),
            "role_in_scenario": actor.get("role_in_scenario"),
            "current_district_id": actor.get("mobility", {}).get("current_district_id"),
            "apps": sorted(actor.get("app_projection", {}).keys()),
            "constraint_ids": [c.get("constraint_id") for c in actor.get("constraints", [])],
            "has_delayed_constraint": any(c.get("visibility") == "delayed" for c in actor.get("constraints", [])),
            "search_text": _join_search_text(
                actor.get("name"),
                actor.get("relationship_to_user"),
                actor.get("preferences", {}),
                actor.get("mobility", {}),
                actor.get("availability", {}),
            ),
        }
    for contact in world.get("peripheral_contacts", []):
        objects[contact["contact_id"]] = {
            **_summary("peripheral_contact", contact["contact_id"], contact.get("name"), contact),
            "relationship": contact.get("relationship_to_user"),
            "rough_district_id": contact.get("rough_district_id"),
            "possible_info_role": contact.get("possible_info_role"),
            "search_text": _join_search_text(contact),
        }
    for persona in world.get("public_personas", []):
        objects[persona["persona_id"]] = {
            **_summary("public_persona", persona["persona_id"], persona.get("persona_type"), persona),
            "persona_type": persona.get("persona_type"),
            "home_or_work_district_id": persona.get("home_or_work_district_id"),
            "credibility": persona.get("credibility"),
            "search_text": _join_search_text(persona),
        }
    for info in world.get("information_items", []):
        objects[info["info_id"]] = {
            **_summary("information", info["info_id"], info.get("content_type"), info),
            "content_type": info.get("content_type"),
            "about": info.get("about", {}),
            "visible_via": info.get("observability", {}).get("visible_via", []),
            "visibility": info.get("observability", {}).get("visibility"),
            "search_text": _join_search_text(info.get("content_type"), info.get("content"), info.get("about", {})),
        }
    for event in world.get("event_templates", []):
        target_refs = [
            transition.get("target_ref")
            for transition in event.get("state_transitions", [])
            if transition.get("target_ref")
        ]
        visible_via = _event_visible_via(event)
        objects[event["event_template_id"]] = {
            **_summary("event", event["event_template_id"], event.get("name"), event),
            "trigger_type": event.get("trigger_type"),
            "scope_type": event.get("scope_type"),
            "target_refs": target_refs,
            "visible_via": visible_via,
            "search_text": _join_search_text(
                event.get("name"),
                event.get("trigger_type"),
                event.get("preconditions", []),
                event.get("director_notes", {}),
            ),
        }
    return objects


def _relation_index(world: dict[str, Any], objects: dict[str, dict[str, Any]]) -> dict[str, Any]:
    del objects
    edges: list[dict[str, Any]] = []
    for place in world.get("places", []):
        _edge(edges, place["place_id"], place.get("district_id"), "in_district")
        for corridor_id in place.get("near_corridor_ids", []):
            _edge(edges, place["place_id"], corridor_id, "near_corridor")
    for service in world.get("services", []):
        _edge(edges, service["service_id"], service.get("place_id"), "service_at_place")
    for corridor in world.get("corridors", []):
        for rel in corridor.get("relations", []):
            _edge(edges, corridor["corridor_id"], rel.get("target_id"), rel.get("relation_type") or "related")
    for actor in world.get("actors", []):
        _edge(edges, actor["actor_id"], actor.get("mobility", {}).get("current_district_id"), "currently_in_district")
        _edge(edges, actor["actor_id"], actor.get("mobility", {}).get("home_district_id"), "home_district")
        for corridor_id in actor.get("mobility", {}).get("sensitive_corridor_ids", []):
            _edge(edges, actor["actor_id"], corridor_id, "sensitive_to_corridor")
        for constraint in actor.get("constraints", []):
            _edge(edges, actor["actor_id"], constraint.get("constraint_id"), "owns_constraint")
    for contact in world.get("peripheral_contacts", []):
        _edge(edges, contact["contact_id"], contact.get("rough_district_id"), "roughly_in_district")
    for persona in world.get("public_personas", []):
        _edge(edges, persona["persona_id"], persona.get("home_or_work_district_id"), "persona_locality")
    for info in world.get("information_items", []):
        for key, target_id in info.get("about", {}).items():
            _edge(edges, info["info_id"], target_id, f"about_{key}")
        author_id = info.get("app_payload", {}).get("author_id")
        if author_id:
            _edge(edges, author_id, info["info_id"], "authored_information")
    for event in world.get("event_templates", []):
        for target_ref in [
            transition.get("target_ref")
            for transition in event.get("state_transitions", [])
            if transition.get("target_ref")
        ]:
            _edge(edges, event["event_template_id"], target_ref, "changes")
        for effect in event.get("information_effects", []):
            _edge(edges, event["event_template_id"], effect.get("info_id"), "reveals_or_updates_information")
    for edge in world.get("social_graph", {}).get("edges", []):
        _edge(
            edges,
            edge.get("source_actor_id"),
            edge.get("target_actor_id"),
            edge.get("relation_type") or "social_relation",
            strength=edge.get("strength"),
            trust=edge.get("trust"),
        )
    adjacency: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        adjacency[edge["source_id"]].append(edge)
        adjacency[edge["target_id"]].append(
            {
                **edge,
                "source_id": edge["target_id"],
                "target_id": edge["source_id"],
                "relation_type": f"inverse:{edge['relation_type']}",
            }
        )
    return {"edges": edges, "adjacency": dict(adjacency)}


def _affordance_index(world: dict[str, Any]) -> dict[str, Any]:
    by_affordance: dict[str, list[str]] = defaultdict(list)
    by_oracle_function: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_app: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for service in world.get("services", []):
        service_id = service["service_id"]
        for affordance in service.get("affordances", []):
            by_affordance[affordance].append(service_id)
        for oracle in service.get("oracle_affordances", []):
            entry = {
                "service_id": service_id,
                "app": oracle.get("app"),
                "function": oracle.get("function"),
                "required_args": oracle.get("required_args", []),
                "judge_strategy": oracle.get("judge_strategy"),
            }
            if oracle.get("function"):
                by_oracle_function[oracle["function"]].append(entry)
            if oracle.get("app"):
                by_app[oracle["app"]].append(entry)
    return {
        "by_affordance": _sorted_lists(by_affordance),
        "by_oracle_function": dict(by_oracle_function),
        "by_app": dict(by_app),
    }


def _dynamic_hook_index(world: dict[str, Any]) -> dict[str, Any]:
    events = []
    by_target: dict[str, list[str]] = defaultdict(list)
    by_trigger: dict[str, list[str]] = defaultdict(list)
    by_app: dict[str, list[str]] = defaultdict(list)
    for event in world.get("event_templates", []):
        event_id = event["event_template_id"]
        target_refs = [
            transition.get("target_ref")
            for transition in event.get("state_transitions", [])
            if transition.get("target_ref")
        ]
        visible_via = _event_visible_via(event)
        summary = {
            "event_template_id": event_id,
            "name": event.get("name"),
            "trigger_type": event.get("trigger_type"),
            "scope_type": event.get("scope_type"),
            "target_refs": target_refs,
            "visible_via": visible_via,
            "preconditions": event.get("preconditions", []),
            "transition_fields": [
                {
                    "target_ref": transition.get("target_ref"),
                    "field_path": transition.get("field_path"),
                    "op": transition.get("op"),
                    "value": transition.get("value"),
                }
                for transition in event.get("state_transitions", [])
            ],
        }
        events.append(summary)
        by_trigger[event.get("trigger_type", "unknown")].append(event_id)
        for target_ref in target_refs:
            by_target[target_ref].append(event_id)
        for app in visible_via:
            by_app[app].append(event_id)
    return {
        "events": events,
        "by_target_ref": _sorted_lists(by_target),
        "by_trigger_type": _sorted_lists(by_trigger),
        "by_visible_app": _sorted_lists(by_app),
    }


def _observability_index(world: dict[str, Any], dynamic_hooks: dict[str, Any]) -> dict[str, Any]:
    by_app: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for info in world.get("information_items", []):
        obs = info.get("observability", {})
        for app in obs.get("visible_via", []):
            bucket = obs.get("visibility") or "visible"
            by_app[app][f"{bucket}_information"].append(info["info_id"])
            by_object[info["info_id"]].append(
                {
                    "channel": app,
                    "visibility": bucket,
                    "available_at": obs.get("available_at"),
                    "confidence": obs.get("confidence"),
                    "staleness": obs.get("staleness"),
                }
            )
    for actor in world.get("actors", []):
        for constraint in actor.get("constraints", []):
            for app in constraint.get("visible_via", []):
                bucket = constraint.get("visibility", "visible")
                by_app[app][f"{bucket}_constraints"].append(constraint.get("constraint_id"))
                by_object[actor["actor_id"]].append(
                    {
                        "channel": app,
                        "visibility": bucket,
                        "constraint_id": constraint.get("constraint_id"),
                    }
                )
    for event in dynamic_hooks.get("events", []):
        for app in event.get("visible_via", []):
            by_app[app]["event_templates"].append(event["event_template_id"])
            by_object[event["event_template_id"]].append(
                {
                    "channel": app,
                    "visibility": "event_effect",
                    "trigger_type": event.get("trigger_type"),
                }
            )
    return {
        "by_app": {app: _sorted_lists(values) for app, values in by_app.items()},
        "by_object": dict(by_object),
    }


def _app_surface_index(world: dict[str, Any], observability: dict[str, Any]) -> dict[str, Any]:
    apps = set(APP_SURFACES)
    city = (world.get("cities") or [{}])[0]
    apps.update(city.get("supported_apps", []))
    apps.update(world.get("social_app_projections", {}).keys())
    apps.update(observability.get("by_app", {}).keys())
    return {
        app: {
            **APP_SURFACES.get(app, {"read": [], "write": [], "observes": []}),
            "observability_counts": {
                key: len(value)
                for key, value in observability.get("by_app", {}).get(app, {}).items()
                if isinstance(value, list)
            },
        }
        for app in sorted(apps)
    }


def _summary(object_type: str, object_id: str, name: str | None, raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "object_id": object_id,
        "object_type": object_type,
        "name": name or object_id,
        "raw_collection": _collection_for_type(object_type),
        "has_director_notes": bool(raw.get("director_notes")),
    }


def _collection_for_type(object_type: str) -> str:
    return {
        "city": "cities",
        "district": "districts",
        "corridor": "corridors",
        "place": "places",
        "service": "services",
        "actor": "actors",
        "peripheral_contact": "peripheral_contacts",
        "public_persona": "public_personas",
        "information": "information_items",
        "event": "event_templates",
    }[object_type]


def _edge(edges: list[dict[str, Any]], source_id: str | None, target_id: str | None, relation_type: str | None, **extra: Any) -> None:
    if not source_id or not target_id:
        return
    edges.append(
        {
            "source_id": source_id,
            "target_id": target_id,
            "relation_type": relation_type or "related",
            **{key: value for key, value in extra.items() if value is not None},
        }
    )


def _event_visible_via(event: dict[str, Any]) -> list[str]:
    apps = set(event.get("app_event_projection", {}).keys())
    for effect in event.get("information_effects", []):
        if effect.get("channel"):
            apps.add(effect["channel"])
    return sorted(apps)


def _sorted_lists(mapping: dict[str, list[Any]]) -> dict[str, list[Any]]:
    return {
        key: sorted(set(value))
        for key, value in mapping.items()
    }


def _join_search_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, dict):
            parts.extend(_join_search_text(k, v) for k, v in value.items())
        elif isinstance(value, list):
            parts.extend(_join_search_text(item) for item in value)
        else:
            parts.append(str(value))
    return " ".join(part for part in parts if part).lower()
