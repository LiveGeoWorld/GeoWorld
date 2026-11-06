"""Validation for v7 world and GeoScenarioIR."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shared.store import WorldStore


@dataclass
class ValidationReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        self.errors.append(message)
        self.ok = False

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


def validate_world(world: dict[str, Any]) -> ValidationReport:
    report = ValidationReport(ok=True)
    store = WorldStore(world)

    for place_id, place in store.places.items():
        did = place.get("district_id")
        if did and did not in store.districts:
            report.add_error(f"Place {place_id} references missing district {did}")
        for cid in place.get("near_corridor_ids", []):
            if cid not in store.corridors:
                report.add_error(f"Place {place_id} references missing corridor {cid}")

    for service_id, service in store.services.items():
        if service.get("place_id") not in store.places:
            report.add_error(
                f"Service {service_id} references missing place {service.get('place_id')}"
            )
        for oracle in service.get("oracle_affordances", []):
            missing = [arg for arg in oracle.get("required_args", []) if not isinstance(arg, str)]
            if missing:
                report.add_error(f"Service {service_id} has invalid required args {missing}")

    for info_id, info in store.information.items():
        about = info.get("about", {})
        for target_type, target_id in about.items():
            collection = _collection_for_about_type(target_type)
            if collection and target_id not in getattr(store, collection):
                report.add_error(f"Information {info_id} references missing {target_type} {target_id}")

    for template_id, template in store.event_templates.items():
        if not template.get("state_transitions") and not template.get("information_effects"):
            report.add_warning(f"EventTemplate {template_id} has no executable effects")

    _validate_social_projections(report, store, world)
    return report


def validate_scenario_ir(world: dict[str, Any], scenario: dict[str, Any]) -> ValidationReport:
    report = validate_world(world)
    store = WorldStore(world)
    world_slice = scenario.get("world_slice", {})

    _check_ids(report, store.districts, "district", world_slice.get("district_ids", []))
    _check_ids(report, store.corridors, "corridor", world_slice.get("corridor_ids", []))
    _check_ids(report, store.places, "place", world_slice.get("place_ids", []))
    _check_ids(report, store.services, "service", world_slice.get("service_ids", []))
    _check_ids(report, store.actors, "actor", world_slice.get("actor_ids", []))
    _check_ids(report, store.information, "information", world_slice.get("information_ids", []))
    _check_ids(
        report,
        store.event_templates,
        "event_template",
        world_slice.get("event_template_ids", []),
    )

    event_ids: set[str] = set()
    for section in ("timeline", "oracle_plan"):
        for event in scenario.get(section, []):
            event_id = event.get("event_id")
            if not event_id:
                report.add_error(f"{section} contains event without event_id")
                continue
            if event_id in event_ids:
                report.add_error(f"Duplicate event id {event_id}")
            event_ids.add(event_id)
            for dep in event.get("dependencies", []):
                if dep not in event_ids and not _dep_exists_later(scenario, dep):
                    report.add_error(f"Event {event_id} depends on unknown event {dep}")

    for oracle_event in scenario.get("oracle_plan", []):
        _validate_oracle_event(report, store, oracle_event)

    if not scenario.get("task_intent", {}).get("user_prompt"):
        report.add_error("ScenarioIR missing task_intent.user_prompt")

    return report


def _collection_for_about_type(target_type: str) -> str | None:
    return {
        "place_id": "places",
        "service_id": "services",
        "actor_id": "actors",
        "district_id": "districts",
        "corridor_id": "corridors",
    }.get(target_type)


def _validate_social_projections(report: ValidationReport, store: WorldStore, world: dict[str, Any]) -> None:
    projections = world.get("social_app_projections")
    if not projections:
        if store.actors:
            report.add_warning("World has actors but no social_app_projections")
        return

    contacts = projections.get("ContactsApp", {}).get("contacts", {})
    users = projections.get("MessagingAppV2", {}).get("id_to_name", {})
    calendar_events = projections.get("CalendarApp", {}).get("events", {})
    if not contacts:
        report.add_warning("ContactsApp projection has no contacts")
    if not users:
        report.add_warning("MessagingAppV2 projection has no users")
    if not calendar_events:
        report.add_warning("CalendarApp projection has no events")

    for actor_id, actor in store.actors.items():
        current_district = actor.get("mobility", {}).get("current_district_id")
        if current_district and current_district not in store.districts:
            report.add_error(f"Actor {actor_id} references missing current_district_id {current_district}")
        home_district = actor.get("mobility", {}).get("home_district_id")
        if home_district and home_district not in store.districts:
            report.add_error(f"Actor {actor_id} references missing home_district_id {home_district}")

        contact_id = actor.get("communication", {}).get("contact_id")
        if contact_id and contact_id not in contacts:
            report.add_error(f"Actor {actor_id} missing ContactsApp contact {contact_id}")
        messaging_user_id = actor.get("communication", {}).get("messaging_user_id")
        if messaging_user_id and messaging_user_id not in users:
            report.add_error(f"Actor {actor_id} missing MessagingAppV2 user {messaging_user_id}")
        calendar_id = f"cal_{actor_id}_availability"
        if calendar_id not in calendar_events:
            report.add_error(f"Actor {actor_id} missing CalendarApp event {calendar_id}")

        delayed_constraints = [
            constraint
            for constraint in actor.get("constraints", [])
            if constraint.get("visibility") == "delayed"
        ]
        if actor.get("communication", {}).get("preferred_channel") == "MessagingAppV2" and not delayed_constraints:
            report.add_warning(f"Actor {actor_id} uses MessagingAppV2 but has no delayed constraint")

    social_quality = world.get("generation_metadata", {}).get("live_overlay", {}).get("social_quality", {})
    for error in social_quality.get("errors", []):
        report.add_error(f"Social quality: {error}")


def _check_ids(report: ValidationReport, mapping: dict[str, Any], label: str, ids: list[str]) -> None:
    for item_id in ids:
        if item_id not in mapping:
            report.add_error(f"Scenario references missing {label} id {item_id}")


def _dep_exists_later(scenario: dict[str, Any], dep: str) -> bool:
    for section in ("timeline", "oracle_plan"):
        if any(event.get("event_id") == dep for event in scenario.get(section, [])):
            return True
    return False


def _validate_oracle_event(report: ValidationReport, store: WorldStore, event: dict[str, Any]) -> None:
    app = event.get("app")
    fn = event.get("function")
    args = event.get("args", {})
    if app == "LocalServiceApp" and fn in {"make_reservation", "cancel_reservation", "modify_reservation"}:
        service_id = args.get("service_id")
        if not service_id:
            report.add_error(f"Oracle {event.get('event_id')} missing service_id")
            return
        if service_id not in store.services:
            report.add_error(f"Oracle {event.get('event_id')} references missing service {service_id}")
            return
        required_affordance = {
            "make_reservation": "reservable",
            "cancel_reservation": "cancelable",
            "modify_reservation": "modifiable",
        }[fn]
        if not store.service_has_affordance(service_id, required_affordance):
            report.add_error(
                f"Oracle {event.get('event_id')} calls {fn} but {service_id} lacks {required_affordance}"
            )
