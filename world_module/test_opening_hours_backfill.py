import copy

import pytest

from shared.io import read_json, write_json
from world_module.opening_hours_backfill import apply_journal, backfill_city, refresh_live_references, verify_supplement_only
from world_module.service_detail_snapshot import sha256_file


def test_only_new_hours_are_allowed():
    original = {"places": [{"place_id": "a", "map_projection": {"google_place_details": {"regular_opening_hours": {}}}}]}
    completed = copy.deepcopy(original)
    completed["places"][0]["map_projection"]["google_place_details"]["agent_inferred_opening_hours"] = {"periods": []}
    verify_supplement_only(original, completed)
    completed["places"][0]["name"] = "changed"
    with pytest.raises(ValueError):
        verify_supplement_only(original, completed)


def test_shared_reference_updated_but_frozen_copy_untouched():
    value = {"shared": {"path": "outputs/world.json", "sha256": "old"},
             "frozen": {"path": "tasks/task_1/world.json", "sha256": "old"}}
    changed = refresh_live_references(value, replacements={"outputs/world.json": {"before": "old", "after": "new", "bytes": 2}},
                                      city_id="city", version=1, artifacts={})
    assert changed["shared"]["sha256"] == "new"
    assert changed["frozen"] == value["frozen"]


def test_backfill_same_version_and_idempotent_resume(tmp_path, monkeypatch):
    import world_module.opening_hours_backfill as module
    world_path = tmp_path / "outputs/worlds/w/map/world.json"
    original = {"cities": [{"city_id": "c"}], "places": [{"place_id": "p", "map_projection": {"google_place_details": {}}}]}
    write_json(world_path, original)
    ref = {"status": "available", "path": world_path.relative_to(tmp_path).as_posix(), "sha256": sha256_file(world_path)}
    record_path = tmp_path / "outputs/worlds/w/cities/c.json"
    record = {"active_version": 1, "artifacts": {"map_world": ref}, "city_versions": [{"version": 1, "artifacts": {"map_world": ref}}]}
    write_json(record_path, record)
    write_json(tmp_path / "outputs/worlds/w/world_manifest.json", {"cities": [{"city_id": "c"}]})
    provenance_path = tmp_path / "outputs/benchmarks/t/w/c/b/world_provenance.json"
    write_json(provenance_path, {"world_id": "w", "world_map_snapshot": {"c": {"path": ref["path"], "sha256": ref["sha256"]}}})
    frozen = tmp_path / "outputs/benchmarks/t/w/c/b/tasks/task_1/world.json"
    write_json(frozen, original)
    def complete(world, **kwargs):
        completed = copy.deepcopy(world)
        completed["places"][0]["map_projection"]["google_place_details"]["agent_inferred_opening_hours"] = {"periods": [{"open": {"day": 0}}]}
        return completed, {"world_binding": {}, "records": []}
    monkeypatch.setattr(module, "complete_world_hours", complete)
    monkeypatch.setattr(module, "build_local_service_world_state", lambda cache, world: {"world_binding": cache["world_binding"]})
    result = backfill_city(tmp_path, "w", "c", tmp_path / "op", endpoint_name="test")
    assert result["status"] == "completed"
    updated = read_json(record_path)
    assert updated["active_version"] == 1
    assert len(updated["city_versions"]) == 1
    assert updated["artifacts"]["map_world"]["sha256"] == sha256_file(world_path)
    assert read_json(provenance_path)["world_map_snapshot"]["c"]["sha256"] == sha256_file(world_path)
    assert read_json(frozen) == original
    assert backfill_city(tmp_path, "w", "c", tmp_path / "op", endpoint_name="test") == result


def test_apply_refuses_concurrent_changes(tmp_path):
    write_json(tmp_path / "world.json", {"concurrent": True})
    operation = tmp_path / "op"
    write_json(operation / "journal.json", {"status": "prepared", "files": [
        {"path": "world.json", "before": "old", "after": "new", "staged": "staged/0.json"}]})
    with pytest.raises(ValueError, match="Concurrent"):
        apply_journal(tmp_path, operation)
