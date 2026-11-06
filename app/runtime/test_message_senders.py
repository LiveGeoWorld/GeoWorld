import json
from copy import deepcopy

import pytest

from app.runtime.message_senders import resolve_message_senders
from app.apps.messaging import MessagingApp


def save(folder, filename, payload):
    (folder / filename).write_text(json.dumps(payload), encoding="utf-8")


def message(scope_type="user_request", scope_id="request"):
    return {"event_id": "e", "app": "MessagingApp", "event_type": "user_constraint_update",
            "scope": {"type": scope_type, "id": scope_id},
            "public_scope": {"name": "Not a person's name: request title"},
            "state_patch": {"content": "Please update my plans."}}


@pytest.mark.parametrize("kind", ["route_planning", "safety_planning", "region_aware", "itinerary"])
def test_recorded_request_actor(kind, tmp_path):
    person = {"character_id": "person_a", "name": "Alice"}
    sandbox = {"source_task": {"task_type": kind, "task_id": "request"}}
    if kind == "safety_planning":
        sandbox["safety_choice"] = {"character": person}
    elif kind != "route_planning":
        sandbox["character"] = person
    save(tmp_path, "task_sandbox.json", sandbox)
    save(tmp_path, "world_slice.json", {"characters": [person]})
    event = message()
    before = deepcopy(event)
    audit = resolve_message_senders([event], [tmp_path])
    assert audit == {"resolved_count": 1, "unresolved_event_ids": []}
    assert event["state_patch"]["sender_name"] == "Alice"
    assert {k: v for k, v in event["state_patch"].items() if k != "sender_name"} == before["state_patch"]
    app = MessagingApp()
    app.inject_entry({**event["state_patch"], "event_type": event["event_type"],
                      "scope": event["scope"], "public_scope": event["public_scope"]})
    public = app.get_user_messages()["messages"][0]
    assert public["from"] == public["sender_name"] == "Alice"
    assert public["conversation"] == event["public_scope"]["name"]
    assert "person_a" not in str(public)


def test_multi_entity_uses_event_actor_not_first_participant(tmp_path):
    participants = [{"character_id": "a", "name": "Alice"}, {"character_id": "b", "name": "Bob"}]
    save(tmp_path, "task_sandbox.json", {"participants": participants,
                                       "source_task": {"task_id": "request"}})
    save(tmp_path, "world_slice.json", {"characters": participants})
    event = message("participant", "b")
    event["event_type"] = "participant_update"
    audit = resolve_message_senders([event], [tmp_path])
    assert audit["resolved_count"] == 1
    assert event["state_patch"]["sender_name"] == "Bob"
    app = MessagingApp()
    app._sandbox = {"participants": participants}
    app.inject_entry({**event["state_patch"], "scope": event["scope"]})
    assert app.get_contact("Bob")["recent_messages"][0]["sender_name"] == "Bob"
    assert not app.get_contact("Alice")["recent_messages"]
    request = message()
    save(tmp_path, "env_events.json", {"env_events": [request]})
    assert resolve_message_senders([request], [tmp_path])["resolved_count"] == 1
    assert request["state_patch"]["sender_name"] == "Alice、Bob"
    app.inject_entry({**request["state_patch"], "event_type": request["event_type"],
                      "scope": request["scope"], "public_scope": request["public_scope"]})
    assert app.get_user_messages()["messages"][0]["from"] == "Alice、Bob"


def test_other_version_cannot_supply_name(tmp_path):
    save(tmp_path, "task_sandbox.json", {"source_task": {"task_id": "request"},
                                       "character": {"character_id": "a", "name": "Alice"}})
    save(tmp_path, "task_provenance.json", {"world_id": "world", "city_id": "city",
                                          "generation_context": {"city_version": "v2"}})
    event = message()
    assert resolve_message_senders([event], [tmp_path], "world/city/v1")["resolved_count"] == 0
    assert event["state_patch"]["sender_name"] == ""


def test_reused_source_id_resolves_each_events_own_batch(tmp_path):
    folders, events = [], []
    for batch, name in [("old", "Alice"), ("new", "Bob")]:
        folder = tmp_path / batch
        folder.mkdir()
        person = {"character_id": batch, "name": name}
        save(folder, "task_sandbox.json", {"source_task": {"task_id": "request"},
                                            "character": person})
        event = message()
        event["event_id"] = batch
        save(folder, "env_events.json", {"env_events": [event]})
        folders.append(folder)
        events.append(event)
    for order in (folders, list(reversed(folders))):
        loaded = deepcopy(events)
        assert resolve_message_senders(loaded, order)["resolved_count"] == 2
        assert [e["state_patch"]["sender_name"] for e in loaded] == ["Alice", "Bob"]
        assert resolve_message_senders([deepcopy(events[1])], order)["resolved_count"] == 1


def test_owned_multi_person_request_does_not_borrow_another_batch_actor(tmp_path):
    single, multi = tmp_path / "single", tmp_path / "multi"
    single.mkdir()
    multi.mkdir()
    alice = {"character_id": "a", "name": "Alice"}
    bob = {"character_id": "b", "name": "Bob"}
    save(single, "task_sandbox.json", {"source_task": {"task_id": "request"}, "character": alice})
    save(multi, "task_sandbox.json", {"source_task": {"task_id": "request"}})
    save(multi, "world_slice.json", {"characters": [alice, bob]})
    event = message()
    save(multi, "env_events.json", {"env_events": [event]})
    assert resolve_message_senders([event], [single, multi])["resolved_count"] == 1
    assert event["state_patch"]["sender_name"] == "Alice、Bob"


def test_multi_request_uses_complete_participants_not_world_slice_order(tmp_path):
    people = [{"character_id": "b", "name": "Bob"}, {"character_id": "a", "name": "Alice"}]
    sandbox = {"source_task": {"task_id": "request", "task_type": "multi_entity"}, "participants": people}
    save(tmp_path, "task_sandbox.json", sandbox)
    save(tmp_path, "world_slice.json", {"characters": list(reversed(people))})
    event = message()
    save(tmp_path, "env_events.json", {"env_events": [event]})
    assert resolve_message_senders([event], [tmp_path])["resolved_count"] == 1
    assert event["state_patch"]["sender_name"] == "Bob、Alice"
    sandbox["participants"][1].pop("name")
    save(tmp_path, "task_sandbox.json", sandbox)
    save(tmp_path, "world_slice.json", {})
    assert resolve_message_senders([event], [tmp_path])["resolved_count"] == 0
    assert event["state_patch"]["sender_name"] == ""


def test_conflicting_event_owners_are_not_resolved_by_same_public_name(tmp_path):
    folders = []
    event = message()
    for actor in ("a", "b"):
        folder = tmp_path / actor
        folder.mkdir()
        save(folder, "task_sandbox.json", {"source_task": {"task_id": "request"},
             "character": {"character_id": actor, "name": "Alice"}})
        save(folder, "env_events.json", [event])
        folders.append(folder)
    assert resolve_message_senders([event], folders)["unresolved_event_ids"] == ["e"]
