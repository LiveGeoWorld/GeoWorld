"""In-memory v7 world store."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorldStore:
    """A normalized lookup layer over a v7 world JSON payload."""

    payload: dict[str, Any]
    cities: dict[str, dict[str, Any]] = field(init=False)
    districts: dict[str, dict[str, Any]] = field(init=False)
    corridors: dict[str, dict[str, Any]] = field(init=False)
    places: dict[str, dict[str, Any]] = field(init=False)
    services: dict[str, dict[str, Any]] = field(init=False)
    actors: dict[str, dict[str, Any]] = field(init=False)
    information: dict[str, dict[str, Any]] = field(init=False)
    event_templates: dict[str, dict[str, Any]] = field(init=False)

    def __post_init__(self) -> None:
        self.cities = self._index("cities", "city_id")
        self.districts = self._index("districts", "district_id")
        self.corridors = self._index("corridors", "corridor_id")
        self.places = self._index("places", "place_id")
        self.services = self._index("services", "service_id")
        self.actors = self._index("actors", "actor_id")
        self.information = self._index("information_items", "info_id")
        self.event_templates = self._index("event_templates", "event_template_id")

    def _index(self, key: str, id_field: str) -> dict[str, dict[str, Any]]:
        items = self.payload.get(key, [])
        return {item[id_field]: item for item in items}

    def require(self, collection: str, item_id: str) -> dict[str, Any]:
        mapping = getattr(self, collection)
        if item_id not in mapping:
            raise KeyError(f"Unknown {collection} id: {item_id}")
        return mapping[item_id]

    def service_has_affordance(self, service_id: str, affordance: str) -> bool:
        service = self.require("services", service_id)
        return affordance in service.get("affordances", [])

    def to_app_initial_state(self, world_slice: dict[str, Any]) -> dict[str, Any]:
        """Project a world slice to generic v7 app-state payloads.

        This is not a final app implementation. It is the deterministic contract
        compiler that future concrete apps should consume.
        """
        place_ids = world_slice.get("place_ids", [])
        service_ids = world_slice.get("service_ids", [])
        actor_ids = world_slice.get("actor_ids", [])
        info_ids = world_slice.get("information_ids", [])
        event_template_ids = world_slice.get("event_template_ids", [])

        places = [self.places[pid] for pid in place_ids]
        services = [self.services[sid] for sid in service_ids]
        actors = [self.actors[aid] for aid in actor_ids]
        infos = [self.information[iid] for iid in info_ids]
        event_templates = [self.event_templates[eid] for eid in event_template_ids]
        social_apps = self.payload.get("social_app_projections", {})
        contacts_app = _slice_contacts_app(social_apps.get("ContactsApp", {}), actors)
        internal_contacts = _slice_contacts_app(social_apps.get("InternalContacts", contacts_app), actors, include_user=True)
        messaging_app = _slice_messaging_app(social_apps.get("MessagingAppV2", {}), actors)
        calendar_app = _slice_calendar_app(social_apps.get("CalendarApp", {}), actors)

        return {
            "MapApp": {
                "places": {
                    p["place_id"]: _place_to_map_payload(p)
                    for p in places
                },
                "districts": {
                    did: self.districts[did]
                    for did in world_slice.get("district_ids", [])
                    if did in self.districts
                },
                "corridors": {
                    cid: self.corridors[cid]
                    for cid in world_slice.get("corridor_ids", [])
                    if cid in self.corridors
                },
                "traffic_alerts": [],
                "transit_alerts": [],
            },
            "LocalServiceApp": {
                "services": {s["service_id"]: s for s in services},
                "reservations": {},
            },
            "ReviewSocialApp": {
                "information_items": {
                    i["info_id"]: i
                    for i in infos
                    if i.get("content_type") in {"review", "post", "social_post", "local_tip", "rumor"}
                }
            },
            "WeatherCityEventApp": {
                "weather": {},
                "city_events": [],
            },
            "ContactsApp": contacts_app,
            "InternalContacts": internal_contacts,
            "MessagingAppV2": messaging_app,
            "ContactsMessagingApp": {
                "actors": {a["actor_id"]: a for a in actors},
                "messages": [],
                "compatibility_note": "legacy v7 projection; prefer ContactsApp and MessagingAppV2",
            },
            "CalendarApp": {
                "events": calendar_app.get("events", {}),
                "availability": {
                    a["actor_id"]: a.get("availability", {})
                    for a in actors
                },
                **{
                    key: value
                    for key, value in calendar_app.items()
                    if key != "events"
                },
            },
            "EventRuntime": {
                "event_templates": {
                    e["event_template_id"]: e for e in event_templates
                }
            },
        }


def _place_to_map_payload(place: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": place["place_id"],
        "name": place["name"],
        "location": place["location"],
        "primary_type": place["primary_type"],
        "address": place.get("address", ""),
        "district_id": place.get("district_id"),
        "near_corridor_ids": place.get("near_corridor_ids", []),
        **place.get("map_projection", {}),
    }


def _slice_contacts_app(app_state: dict[str, Any], actors: list[dict[str, Any]], *, include_user: bool = True) -> dict[str, Any]:
    contacts = app_state.get("contacts", {})
    wanted = {
        actor.get("communication", {}).get("contact_id")
        for actor in actors
        if actor.get("communication", {}).get("contact_id")
    }
    if include_user:
        wanted.update(contact_id for contact_id, contact in contacts.items() if contact.get("is_user"))
    selected = {
        contact_id: contact
        for contact_id, contact in contacts.items()
        if contact_id in wanted
    }
    return {
        "contacts": selected,
        "view_limit": app_state.get("view_limit", max(10, len(selected))),
    }


def _slice_messaging_app(app_state: dict[str, Any], actors: list[dict[str, Any]]) -> dict[str, Any]:
    current_user_id = app_state.get("current_user_id", "user_me")
    actor_user_ids = {
        actor.get("communication", {}).get("messaging_user_id")
        for actor in actors
        if actor.get("communication", {}).get("messaging_user_id")
    }
    wanted_ids = {current_user_id, *actor_user_ids}
    conversations = {
        conversation_id: conversation
        for conversation_id, conversation in app_state.get("conversations", {}).items()
        if any(participant_id in actor_user_ids for participant_id in conversation.get("participant_ids", []))
    }
    id_to_name = {
        user_id: name
        for user_id, name in app_state.get("id_to_name", {}).items()
        if user_id in wanted_ids
    }
    name_to_id = {
        name: user_id
        for name, user_id in app_state.get("name_to_id", {}).items()
        if user_id in wanted_ids
    }
    return {
        "current_user_id": current_user_id,
        "current_user_name": app_state.get("current_user_name", "Me"),
        "mode": app_state.get("mode", "NAME"),
        "id_to_name": id_to_name,
        "name_to_id": name_to_id,
        "conversations": conversations,
        "messages_view_limit": app_state.get("messages_view_limit", 10),
        "conversation_view_limit": app_state.get("conversation_view_limit", 5),
    }


def _slice_calendar_app(app_state: dict[str, Any], actors: list[dict[str, Any]]) -> dict[str, Any]:
    wanted_event_ids = {
        f"cal_{actor['actor_id']}_availability"
        for actor in actors
        if actor.get("actor_id")
    }
    events = {
        event_id: event
        for event_id, event in app_state.get("events", {}).items()
        if event_id in wanted_event_ids
    }
    return {"events": events}
