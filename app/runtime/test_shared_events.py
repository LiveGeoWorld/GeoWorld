import json
import tempfile
import unittest
from pathlib import Path

from app.runtime.shared_events import load_world_events
from app.runtime.environment import Environment
from app.apps.messaging import MessagingApp


class SharedEventsTests(unittest.TestCase):
    def test_modes_isolate_state_and_preserve_time_and_event_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            city = Path(temp) / "world_002" / "city"
            task = city / "batch" / "tasks" / "task"
            task.mkdir(parents=True)
            (task / "task_provenance.json").write_text(json.dumps({
                "world_id": "world_002", "city_id": "city",
                "generation_context": {"city_version": "v1"}}))
            def event(key):
                return {"event_id": key, "world_binding": "world_002/city/v1",
                        "app": "MessagingApp", "event_type": "user_constraint_update",
                        "activation_time": "2026-09-25T10:00:00",
                        "public_scope": {"name": key},
                        "state_patch": {"content": key}}
            own, other = event("own"), event("other")
            (city / "city_event_ledger.json").write_text(json.dumps({
                "city_id": "city", "events": [own, other]}))
            shared, _, shared_audit = load_world_events(task, [own])
            isolated, _, audit = load_world_events(task, [own], dynamic_event_mode="task_only")
            self.assertEqual(shared[0], isolated[0])
            self.assertEqual([e["event_id"] for e in isolated], ["own"])
            self.assertEqual(audit["loaded_event_count"], 1)
            self.assertEqual(audit["world_event_count"], 2)
            self.assertEqual(shared_audit["dynamic_event_mode"], "shared_world")
            for events, expected in ((shared, 2), (isolated, 1), (shared, 2)):
                env = Environment()
                app = MessagingApp()
                env.register_app(app)
                env.load_dynamic_events(events, required_event_ids={"own"})
                env.init_events("2026-09-25T09:00:00", planning_deadline="2026-09-25T11:00:00")
                self.assertEqual(app.get_user_messages()["count"], 0)
                self.assertTrue(env.pending_required_dynamic_events())
                env.advance_virtual_time("2026-09-25T10:00:00")
                self.assertEqual(app.get_user_messages()["count"], expected)
                self.assertFalse(env.pending_required_dynamic_events())
            own["causal_parents"] = ["other"]
            (city / "city_event_ledger.json").write_text(json.dumps({
                "city_id": "city", "events": [own, other]}))
            with self.assertRaisesRegex(ValueError, "causal parents outside task"):
                load_world_events(task, [own], dynamic_event_mode="task_only")

    def test_unknown_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported dynamic_event_mode"):
            load_world_events(Path("."), [], dynamic_event_mode="unknown")

    def test_shared_pool_is_not_task_pool_and_is_version_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            city = Path(temp) / "world_002" / "city"
            task = city / "batch" / "tasks" / "task"
            task.mkdir(parents=True)
            (task / "task_provenance.json").write_text(json.dumps({
                "world_id": "world_002", "city_id": "city",
                "generation_context": {"city_version": "v1"},
            }))
            def event(key, version="v1"):
                return {"event_id": key, "world_binding": f"world_002/city/{version}",
                        "app": "MessagingApp", "event_type": "user_constraint_update",
                        "activation_time": "2026-09-25T10:00:00",
                        "public_scope": {"name": key + " conversation"},
                        "state_patch": {"content": key + " public message"}}
            own = event("own")
            ledger = city / "city_event_ledger.json"
            ledger.write_text(json.dumps({"city_id": "city", "events": [
                own, event("other"), event("foreign", "v2")]}))
            world, private, audit = load_world_events(task, [own])
            self.assertEqual([e["event_id"] for e in world], ["own", "other"])
            self.assertEqual([e["event_id"] for e in private], ["own"])
            self.assertEqual(audit["world_event_count"], 2)
            env = Environment()
            app = MessagingApp()
            env.register_app(app)
            env.load_dynamic_events(world)
            env.init_events("2026-09-25T09:00:00")
            self.assertEqual(app.get_user_messages()["count"], 0)
            env.advance_virtual_time("2026-09-25T10:00:00", reason="test")
            messages = app.get_user_messages()["messages"]
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[1]["conversation"], "other conversation")
            self.assertNotIn("world_binding", str(messages))
            ledger.unlink()
            with self.assertRaises(FileNotFoundError):
                load_world_events(task, [own])

    def test_unbound_legacy_is_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, audit = load_world_events(Path(temp), [])
        self.assertEqual(audit["event_source"], "legacy_task_local")

    def test_batch_snapshot_is_preferred_over_stale_city_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            city = Path(temp) / "world_002" / "city"
            batch = city / "20260926_172721"
            task = batch / "tasks" / "task"
            task.mkdir(parents=True)
            (task / "task_provenance.json").write_text(json.dumps({
                "world_id": "world_002", "city_id": "city",
                "generation_context": {"city_version": "v1"},
            }))
            def event(key):
                return {"event_id": key, "world_binding": "world_002/city/v1",
                        "app": "MessagingApp", "event_type": "user_constraint_update",
                        "activation_time": "2026-09-25T10:00:00",
                        "public_scope": {"name": key},
                        "state_patch": {"content": key}}
            own = event("batch_event")
            (city / "city_event_ledger.json").write_text(
                json.dumps({"city_id": "city", "events": [event("stale_event")]})
            )
            (batch / "city_event_ledger.json").write_text(
                json.dumps({"city_id": "city", "events": [own]})
            )
            world, private, audit = load_world_events(task, [own])
            self.assertEqual([e["event_id"] for e in world], ["batch_event"])
            self.assertEqual([e["event_id"] for e in private], ["batch_event"])
            self.assertEqual(audit["ledger_path"], str(batch / "city_event_ledger.json"))

    def test_formal_batch_reports_stale_city_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            city = Path(temp) / "world_002" / "city"
            batch = city / "20260926_172721"
            task = batch / "tasks" / "task"
            task.mkdir(parents=True)
            (batch / "world_provenance.json").write_text("{}")
            (task / "task_provenance.json").write_text(json.dumps({
                "world_id": "world_002", "city_id": "city",
                "generation_context": {"city_version": "v1"},
            }))
            (city / "city_event_ledger.json").write_text(json.dumps({
                "city_id": "city", "events": []
            }))
            with self.assertRaisesRegex(ValueError, "batch shared ledger.*does not contain"):
                load_world_events(task, [{
                    "event_id": "missing_task_event", "app": "CityNewsApp",
                    "activation_time": "2026-09-25T10:00:00",
                    "state_patch": {"description": "Missing notice"},
                }])

    def test_shared_person_message_is_queryable_by_public_name(self):
        app = MessagingApp()
        app.inject_entry({"scope": {"type": "participant", "id": "private_person"},
                          "public_scope": {"name": "Alice"}, "content": "Running late"})
        self.assertEqual(app.search_contacts()["contacts"], [{"name": "Alice"}])
        self.assertEqual(app.get_contact("Alice")["recent_messages"][0]["from"], "Alice")


if __name__ == "__main__":
    unittest.main()
