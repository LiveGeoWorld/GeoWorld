"""Regression tests for non-destructive world registry migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from world_module import registry


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _world(path: Path, city_id: str, city_name: str) -> None:
    _write_json(path, {
        "cities": [{"city_id": city_id, "name": city_name, "timezone": "UTC"}],
        "districts": [{"district_id": "d1"}],
        "corridors": [],
        "places": [{"place_id": "p1"}],
        "services": [],
        "information_items": [],
        "event_templates": [],
        "actors": [],
    })


def test_city_record_never_attaches_mismatched_characters(tmp_path: Path, monkeypatch) -> None:
    world_path = tmp_path / "outputs" / "mapseeds" / "tokyo_complex" / "complex" / "world.json"
    _world(world_path, "tokyo_complex", "Tokyo")
    character_path = tmp_path / "outputs" / "characters" / "tokyo" / "complex" / "characters.json"
    _write_json(character_path, {"city_id": "tokyo", "characters": [{"id": "c1"}]})

    monkeypatch.setattr(registry, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(registry, "ROUTE_ROOT", tmp_path / "outputs" / "routes")
    monkeypatch.setattr(registry, "CHARACTER_ROOT", tmp_path / "outputs" / "characters")
    monkeypatch.setattr(registry, "CULTURE_ROOT", tmp_path / "outputs" / "culture")
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", tmp_path / "outputs" / "evidence")

    record = registry._build_city_record("tokyo_complex", world_path)

    assert record["artifacts"]["characters"]["status"] == "missing"
    assert record["artifacts"]["characters"]["legacy_candidates"] == [{
        "path": "outputs/characters/tokyo/complex/characters.json", "city_id": "tokyo",
    }]


def test_cultural_resolution_prefers_complete_canonical_pair_over_exact_partial(
    tmp_path: Path, monkeypatch
) -> None:
    culture_root = tmp_path / "outputs" / "cultural_knowledge"
    evidence_root = tmp_path / "outputs" / "evidence"
    _write_json(evidence_root / "tokyo_complex.json", {"cards": []})
    _write_json(culture_root / "tokyo.json", {"constraints": []})
    _write_json(evidence_root / "tokyo.json", {"cards": []})
    monkeypatch.setattr(registry, "CULTURE_ROOT", culture_root)
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", evidence_root)

    constraint, evidence, source_city_id = registry._resolve_cultural_artifacts("tokyo_complex")

    assert source_city_id == "tokyo"
    assert constraint == culture_root / "tokyo.json"
    assert evidence == evidence_root / "tokyo.json"


def test_cultural_backfill_updates_active_record_and_version_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    world_root = tmp_path / "outputs" / "worlds"
    culture_root = tmp_path / "outputs" / "benchmarks" / "region_aware" / "cultural_constraints"
    evidence_root = tmp_path / "outputs" / "benchmarks" / "region_aware" / "cultural_sources" / "wikivoyage"
    _write_json(culture_root / "city_a.json", {"constraints": []})
    _write_json(evidence_root / "city_a.json", {"cards": []})
    city_path = world_root / "world_test" / "cities" / "city_a.json"
    missing = {"status": "missing", "reason": "legacy"}
    _write_json(city_path, {
        "city_id": "city_a",
        "city_name": "City A",
        "active_version": 1,
        "artifacts": {"cultural_constraints": missing, "cultural_evidence": missing},
        "capabilities": {"regional_knowledge": False},
        "city_versions": [{
            "version": 1,
            "version_id": "v1",
            "artifacts": {"cultural_constraints": missing, "cultural_evidence": missing},
            "counts": {},
        }],
    })
    _write_json(world_root / "world_test" / "world_manifest.json", {
        "world_id": "world_test",
        "cities": [{
            "city_id": "city_a",
            "registry_path": "outputs/worlds/world_test/cities/city_a.json",
        }],
    })
    monkeypatch.setattr(registry, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(registry, "WORLD_ROOT", world_root)
    monkeypatch.setattr(registry, "CULTURE_ROOT", culture_root)
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(registry, "ensure_cultural_assets", lambda *_: {"status": "available"})

    registry.refresh_city_cultural_assets("world_test", "city_a")
    refreshed = json.loads(city_path.read_text(encoding="utf-8"))

    assert refreshed["artifacts"]["cultural_constraints"]["status"] == "available"
    assert refreshed["city_versions"][0]["artifacts"]["cultural_constraints"]["status"] == "available"
    assert refreshed["capabilities"]["regional_knowledge"] is True


def test_missing_service_correction_keeps_city_version_and_records_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    world_root = tmp_path / "outputs" / "worlds"
    world_path = world_root / "world_test" / "artifacts" / "city_a" / "v1" / "world.json"
    source_world = {
        "cities": [{"city_id": "city_a", "name": "City A"}],
        "districts": [], "corridors": [],
        "places": [{"place_id": "p1", "primary_type": "restaurant", "map_projection": {"types": ["restaurant"]}}],
        "services": [], "information_items": [], "event_templates": [],
    }
    _write_json(world_path, source_world)
    old_hash = registry.sha256_file(world_path)
    city_path = world_root / "world_test" / "cities" / "city_a.json"
    artifacts = {"map_world": {"status": "available", "path": "outputs/worlds/world_test/artifacts/city_a/v1/world.json"}}
    _write_json(city_path, {
        "city_id": "city_a", "city_name": "City A", "active_version": 1,
        "artifacts": artifacts,
        "city_versions": [{"version": 1, "version_id": "v1", "artifacts": artifacts, "counts": {}}],
    })
    _write_json(world_root / "world_test" / "world_manifest.json", {
        "world_id": "world_test", "cities": [{"city_id": "city_a", "registry_path": "outputs/worlds/world_test/cities/city_a.json"}],
    })
    provenance_path = tmp_path / "outputs" / "benchmarks" / "multi_entity" / "city_a" / "batch" / "world_provenance.json"
    _write_json(provenance_path, {"world_id": "world_test", "city_id": "city_a", "city_version": "v1", "artifact_snapshot": {"map_world": old_hash}})
    _write_json(provenance_path.parent / "tasks" / "task_0001" / "task.json", {"task_id": "unchanged"})
    monkeypatch.setattr(registry, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(registry, "WORLD_ROOT", world_root)
    monkeypatch.setattr(registry, "BENCHMARK_ROOT", tmp_path / "outputs" / "benchmarks")

    result = registry.repair_missing_factual_services("world_test")
    corrected_world = json.loads(world_path.read_text(encoding="utf-8"))
    corrected_city = json.loads(city_path.read_text(encoding="utf-8"))
    corrected_provenance = json.loads(provenance_path.read_text(encoding="utf-8"))

    assert result["corrections"][0]["versions"][0]["services"] == 1
    assert corrected_world["services"][0]["service_type"] == "restaurant"
    assert [entry["version_id"] for entry in corrected_city["city_versions"]] == ["v1"]
    assert corrected_city["active_version"] == 1
    assert corrected_city["artifacts"]["factual_service_index"]["status"] == "available"
    assert corrected_provenance["artifact_snapshot"]["map_world"] != old_hash
    assert corrected_provenance["derived_data_corrections"][0]["before_map_sha256"] == old_hash
    assert json.loads((provenance_path.parent / "tasks" / "task_0001" / "task.json").read_text(encoding="utf-8")) == {"task_id": "unchanged"}


def test_legacy_import_writes_sidecars_without_mutating_task_payload(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "outputs" / "mapseeds" / "city_a" / "base" / "world.json"
    _world(source, "city_a", "City A")
    task_path = tmp_path / "outputs" / "benchmarks" / "route_planning" / "city_a" / "batch_001" / "tasks" / "task_0001" / "task.json"
    original_task = {"task_id": "task_0001", "task_type": "route_planning", "user_prompt": "unchanged"}
    _write_json(task_path, original_task)

    monkeypatch.setattr(registry, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(registry, "WORLD_ROOT", tmp_path / "outputs" / "worlds")
    monkeypatch.setattr(registry, "MAPSEED_ROOT", tmp_path / "outputs" / "mapseeds")
    monkeypatch.setattr(registry, "ROUTE_ROOT", tmp_path / "outputs" / "routes")
    monkeypatch.setattr(registry, "CHARACTER_ROOT", tmp_path / "outputs" / "characters")
    monkeypatch.setattr(registry, "BENCHMARK_ROOT", tmp_path / "outputs" / "benchmarks")
    monkeypatch.setattr(registry, "CULTURE_ROOT", tmp_path / "outputs" / "culture")
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", tmp_path / "outputs" / "evidence")
    monkeypatch.setattr(registry, "DEFAULT_CITY_WORLD_SOURCES", {"city_a": "outputs/mapseeds/city_a/base/world.json"})

    report = registry.write_legacy_world_import("world_test")

    provenance = tmp_path / "outputs" / "benchmarks" / "route_planning" / "city_a" / "batch_001" / "world_provenance.json"
    assert report["provenance_written"] == ["outputs/benchmarks/route_planning/city_a/batch_001/world_provenance.json"]
    assert json.loads(task_path.read_text(encoding="utf-8")) == original_task
    assert json.loads(provenance.read_text(encoding="utf-8"))["world_id"] == "world_test"
    assert (tmp_path / "outputs" / "worlds" / "world_test" / "cities" / "city_a.json").is_file()


def test_empty_world_can_attach_existing_city_without_copying_source(tmp_path: Path, monkeypatch, local_city_culture) -> None:
    source = tmp_path / "outputs" / "mapseeds" / "city_a" / "base" / "world.json"
    _world(source, "city_a", "City A")

    monkeypatch.setattr(registry, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(registry, "WORLD_ROOT", tmp_path / "outputs" / "worlds")
    monkeypatch.setattr(registry, "ROUTE_ROOT", tmp_path / "outputs" / "routes")
    monkeypatch.setattr(registry, "CHARACTER_ROOT", tmp_path / "outputs" / "characters")
    monkeypatch.setattr(registry, "CULTURE_ROOT", tmp_path / "outputs" / "culture")
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", tmp_path / "outputs" / "evidence")

    created = registry.create_empty_world("world_new", display_name="New World")
    attached = registry.attach_city_artifact("world_new", "city_a", source)
    manifest = json.loads((tmp_path / "outputs" / "worlds" / "world_new" / "world_manifest.json").read_text(encoding="utf-8"))

    assert created["city_count"] == 0
    assert attached["city_name"] == "City A"
    assert attached["artifacts"]["cultural_evidence"]["status"] == "available"
    evidence = json.loads((tmp_path / attached["artifacts"]["cultural_evidence"]["path"]).read_text(encoding="utf-8"))
    assert evidence["evidence_cards"] == local_city_culture["evidence_cards"]
    assert manifest["city_ids"] == ["city_a"]
    assert json.loads(source.read_text(encoding="utf-8"))["cities"][0]["city_id"] == "city_a"


def test_world_bound_expansion_never_reuses_global_route_cache(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "outputs" / "worlds" / "world_new" / "artifacts" / "cities" / "city_a" / "v2" / "world.json"
    _world(source, "city_a", "City A")
    global_route = tmp_path / "outputs" / "routes" / "city_a" / "route_cache.json"
    _write_json(global_route, {"city_id": "city_a", "cache": [{"id": "legacy"}]})

    monkeypatch.setattr(registry, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(registry, "WORLD_ROOT", tmp_path / "outputs" / "worlds")
    monkeypatch.setattr(registry, "ROUTE_ROOT", tmp_path / "outputs" / "routes")
    monkeypatch.setattr(registry, "CHARACTER_ROOT", tmp_path / "outputs" / "characters")
    monkeypatch.setattr(registry, "CULTURE_ROOT", tmp_path / "outputs" / "culture")
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", tmp_path / "outputs" / "evidence")

    record = registry._build_city_record(
        "city_a", source, world_id="world_new", import_mode="city_expansion",
    )

    assert record["artifacts"]["route_cache"]["status"] == "missing"
    assert "world_new" in record["artifacts"]["route_cache"]["path"]


def test_refresh_versioned_route_cache_keeps_the_existing_city_version(tmp_path: Path, monkeypatch, local_city_culture) -> None:
    source = tmp_path / "outputs" / "worlds" / "world_new" / "artifacts" / "cities" / "city_a" / "v2" / "world.json"
    _world(source, "city_a", "City A")
    monkeypatch.setattr(registry, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(registry, "WORLD_ROOT", tmp_path / "outputs" / "worlds")
    monkeypatch.setattr(registry, "ROUTE_ROOT", tmp_path / "outputs" / "routes")
    monkeypatch.setattr(registry, "CHARACTER_ROOT", tmp_path / "outputs" / "characters")
    monkeypatch.setattr(registry, "CULTURE_ROOT", tmp_path / "outputs" / "culture")
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", tmp_path / "outputs" / "evidence")

    registry.create_empty_world("world_new")
    registry.attach_city_artifact("world_new", "city_a", source, city_version=2)
    cache = tmp_path / "outputs" / "worlds" / "world_new" / "artifacts" / "routes" / "city_a" / "v2" / "route_cache.json"
    _write_json(cache, {"city_id": "city_a", "cached_pair_count": 1, "cache": [{"route_id": "r1"}]})

    result = registry.refresh_city_route_cache_asset("world_new", "city_a")
    record = json.loads((tmp_path / "outputs" / "worlds" / "world_new" / "cities" / "city_a.json").read_text(encoding="utf-8"))

    assert result["updated"] is True
    assert record["active_version"] == 2
    assert [entry["version_id"] for entry in record["city_versions"]] == ["v2"]
    assert record["artifacts"]["route_cache"]["status"] == "available"
    assert record["artifacts"]["route_cache"]["path"].endswith("routes/city_a/v2/route_cache.json")


def test_repair_task_batch_provenance_only_adds_missing_sidecars(tmp_path: Path) -> None:
    batch = tmp_path / "batch_a"
    task = batch / "tasks" / "task_0001"
    task.mkdir(parents=True)
    _write_json(task / "task.json", {"task_id": "task_a", "task_type": "place_grounding", "generated_at": "2026-07-24T01:00:00+00:00"})
    _write_json(batch / "world_provenance.json", {
        "world_id": "world_002", "city_id": "city_a", "city_version": "v2",
        "task_type": "place_grounding", "batch_id": "batch_a",
        "required_city_versions": {"city_a": 2}, "world_city_versions": {"city_a": 2},
        "artifact_snapshot": {"map_world": "hash"}, "created_at": "2026-07-24T01:00:00+00:00",
    })

    result = registry.repair_task_batch_provenance(batch)
    task_provenance = json.loads((task / "task_provenance.json").read_text(encoding="utf-8"))
    second = registry.repair_task_batch_provenance(batch)

    assert result["repaired"] == ["task_0001"]
    assert task_provenance["world_id"] == "world_002"
    assert task_provenance["generation_context"]["reconstruction"]["status"] == "reconstructed_from_batch_binding"
    assert second["repaired"] == []
    assert second["skipped"] == ["task_0001"]


def test_per_task_provenance_reuses_one_batch_generation_context(tmp_path: Path, monkeypatch, local_city_culture) -> None:
    """Formalizing sibling tasks must not append duplicate batch contexts."""
    source = tmp_path / "outputs" / "mapseeds" / "city_a" / "base" / "world.json"
    _world(source, "city_a", "City A")
    world_root = tmp_path / "outputs" / "worlds"
    monkeypatch.setattr(registry, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(registry, "WORLD_ROOT", world_root)
    monkeypatch.setattr(registry, "ROUTE_ROOT", tmp_path / "outputs" / "routes")
    monkeypatch.setattr(registry, "CHARACTER_ROOT", tmp_path / "outputs" / "characters")
    monkeypatch.setattr(registry, "CULTURE_ROOT", tmp_path / "outputs" / "culture")
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", tmp_path / "outputs" / "evidence")

    registry.create_empty_world("world_new")
    registry.attach_city_artifact("world_new", "city_a", source, import_mode="legacy_import")
    batch = tmp_path / "outputs" / "benchmarks" / "place_grounding" / "world_new" / "city_a" / "batch_001"
    initial = registry.write_task_world_provenance(
        "world_new", "city_a", batch, task_type="place_grounding", mode="new",
    )
    context = initial["generation_contexts"][0]
    task_paths = []
    for name in ("task_0001", "task_0002"):
        task_path = batch / "tasks" / name
        _write_json(task_path / "task.json", {"task_id": name, "task_type": "place_grounding"})
        task_paths.append(task_path)
        registry.write_task_world_provenance(
            "world_new", "city_a", batch, task_type="place_grounding", mode="new",
            task_folders=[task_path], generation_context=context,
        )

    batch_provenance = json.loads((batch / "world_provenance.json").read_text(encoding="utf-8"))
    task_provenances = [
        json.loads((path / "task_provenance.json").read_text(encoding="utf-8"))
        for path in task_paths
    ]

    assert len(batch_provenance["generation_contexts"]) == 1
    assert batch_provenance["task_count"] == 2
    assert {item["generation_context"]["generation_id"] for item in task_provenances} == {
        context["generation_id"]
    }


def test_task_provenance_initializes_an_empty_batch_tasks_directory(tmp_path: Path, monkeypatch) -> None:
    """The first batch audit must succeed before a candidate task exists."""
    source = tmp_path / "outputs" / "mapseeds" / "city_a" / "base" / "world.json"
    _world(source, "city_a", "City A")
    world_root = tmp_path / "outputs" / "worlds"
    monkeypatch.setattr(registry, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(registry, "WORLD_ROOT", world_root)
    monkeypatch.setattr(registry, "ROUTE_ROOT", tmp_path / "outputs" / "routes")
    monkeypatch.setattr(registry, "CHARACTER_ROOT", tmp_path / "outputs" / "characters")
    monkeypatch.setattr(registry, "CULTURE_ROOT", tmp_path / "outputs" / "culture")
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", tmp_path / "outputs" / "evidence")

    registry.create_empty_world("world_new")
    registry.attach_city_artifact("world_new", "city_a", source, import_mode="legacy_import")
    batch = tmp_path / "outputs" / "benchmarks" / "place_grounding" / "world_new" / "city_a" / "new_batch"

    provenance = registry.write_task_world_provenance(
        "world_new", "city_a", batch, task_type="place_grounding", mode="new",
    )

    assert (batch / "tasks").is_dir()
    assert provenance["task_count"] == 0


def test_world_bound_expansion_uses_world_local_cultural_snapshot(tmp_path: Path, monkeypatch) -> None:
    world_root = tmp_path / "outputs" / "worlds"
    local_root = world_root / "world_new" / "artifacts" / "cultural" / "city_a"
    _write_json(local_root / "cultural_constraints.json", {"constraints": [{"id": "local"}]})
    _write_json(local_root / "wikivoyage_evidence.json", {"evidence_cards": [{"id": "local"}]})
    global_culture = tmp_path / "outputs" / "culture"
    global_evidence = tmp_path / "outputs" / "evidence"
    _write_json(global_culture / "city_a.json", {"constraints": [{"id": "global"}]})
    _write_json(global_evidence / "city_a.json", {"evidence_cards": [{"id": "global"}]})

    monkeypatch.setattr(registry, "WORLD_ROOT", world_root)
    monkeypatch.setattr(registry, "CULTURE_ROOT", global_culture)
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", global_evidence)

    constraint, evidence, source_city_id = registry._resolve_cultural_artifacts(
        "city_a", world_id="world_new", import_mode="city_expansion",
    )

    assert source_city_id == "city_a"
    assert constraint == local_root / "cultural_constraints.json"
    assert evidence == local_root / "wikivoyage_evidence.json"


def test_attach_records_user_confirmed_actual_counts_on_city_version(tmp_path: Path, monkeypatch, local_city_culture) -> None:
    source = tmp_path / "outputs" / "mapseeds" / "city_a" / "base" / "world.json"
    _world(source, "city_a", "City A")
    monkeypatch.setattr(registry, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(registry, "WORLD_ROOT", tmp_path / "outputs" / "worlds")
    monkeypatch.setattr(registry, "ROUTE_ROOT", tmp_path / "outputs" / "routes")
    monkeypatch.setattr(registry, "CHARACTER_ROOT", tmp_path / "outputs" / "characters")
    monkeypatch.setattr(registry, "CULTURE_ROOT", tmp_path / "outputs" / "culture")
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", tmp_path / "outputs" / "evidence")

    registry.create_empty_world("world_new")
    registry.attach_city_artifact(
        "world_new", "city_a", source,
        registration_metadata={
            "mode": "user_confirmed_actual", "requested_targets": {"characters": 3},
            "actual_counts": {"characters": 2}, "target_gaps": {"characters": 1},
        },
    )
    city = json.loads((tmp_path / "outputs" / "worlds" / "world_new" / "cities" / "city_a.json").read_text(encoding="utf-8"))

    assert city["city_versions"][0]["registration_metadata"]["mode"] == "user_confirmed_actual"
    assert city["city_versions"][0]["registration_metadata"]["target_gaps"] == {"characters": 1}


def test_city_versions_are_snapshot_based_and_switch_without_rewriting_sources(tmp_path: Path, monkeypatch, local_city_culture) -> None:
    v1_source = tmp_path / "outputs" / "mapseeds" / "city_a" / "base" / "world.json"
    v2_source = tmp_path / "outputs" / "worlds" / "world_new" / "artifacts" / "city_a_v2.json"
    _world(v1_source, "city_a", "City A")
    _write_json(v2_source, {
        **json.loads(v1_source.read_text(encoding="utf-8")),
        "places": [{"place_id": "p1"}, {"place_id": "p2", "introduced_version": 2}],
    })
    original_source = v1_source.read_text(encoding="utf-8")

    monkeypatch.setattr(registry, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(registry, "WORLD_ROOT", tmp_path / "outputs" / "worlds")
    monkeypatch.setattr(registry, "ROUTE_ROOT", tmp_path / "outputs" / "routes")
    monkeypatch.setattr(registry, "CHARACTER_ROOT", tmp_path / "outputs" / "characters")
    monkeypatch.setattr(registry, "CULTURE_ROOT", tmp_path / "outputs" / "culture")
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", tmp_path / "outputs" / "evidence")

    registry.create_empty_world("world_new")
    registry.attach_city_artifact("world_new", "city_a", v1_source)
    registry.attach_city_artifact("world_new", "city_a", v2_source, city_version=2)
    selected = registry.select_city_version("world_new", "city_a", 1)
    city = json.loads((tmp_path / "outputs" / "worlds" / "world_new" / "cities" / "city_a.json").read_text(encoding="utf-8"))

    assert selected["active_version"] == 1
    assert [entry["version_id"] for entry in city["city_versions"]] == ["v1", "v2"]
    assert city["artifacts"]["map_world"]["path"] == "outputs/mapseeds/city_a/base/world.json"
    assert v1_source.read_text(encoding="utf-8") == original_source


def test_real_city_expansion_inherits_parent_routes_without_external_requests(tmp_path: Path, monkeypatch) -> None:
    world_root = tmp_path / "outputs" / "worlds"
    v1_source = world_root / "world_new" / "artifacts" / "cities" / "city_a" / "v1" / "world.json"
    v2_source = world_root / "world_new" / "artifacts" / "cities" / "city_a" / "v2" / "world.json"
    _world(v1_source, "city_a", "City A")
    v1 = json.loads(v1_source.read_text(encoding="utf-8"))
    v1["places"] = [
        {"place_id": "a", "name": "A", "location": {"lat": 1.0, "lng": 2.0}},
        {"place_id": "b", "name": "B", "location": {"lat": 1.1, "lng": 2.1}},
    ]
    _write_json(v1_source, v1)
    _write_json(v2_source, {
        **v1,
        "places": [
            {"place_id": "a_new", "name": "A", "location": {"lat": 1.0, "lng": 2.0}},
            v1["places"][1],
            {"place_id": "c", "name": "C", "location": {"lat": 1.2, "lng": 2.2}},
        ],
    })
    monkeypatch.setattr(registry, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(registry, "WORLD_ROOT", world_root)
    monkeypatch.setattr(registry, "ROUTE_ROOT", tmp_path / "outputs" / "routes")
    monkeypatch.setattr(registry, "CHARACTER_ROOT", tmp_path / "outputs" / "characters")
    monkeypatch.setattr(registry, "CULTURE_ROOT", tmp_path / "outputs" / "culture")
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", tmp_path / "outputs" / "evidence")
    registry.create_empty_world("world_new")
    registry.attach_city_artifact("world_new", "city_a", v1_source, import_mode="city_expansion")
    parent_cache = world_root / "world_new" / "artifacts" / "routes" / "city_a" / "v1" / "route_cache.json"
    _write_json(parent_cache, {"city_id": "city_a", "cache": [{
        "route_id": "old", "origin_place_id": "a", "origin_name": "A",
        "origin_lat": 1.0, "origin_lng": 2.0, "dest_place_id": "b", "dest_name": "B",
        "dest_lat": 1.1, "dest_lng": 2.1, "mode": "DRIVE", "snapshot_id": "static_v1",
        "waypoint_place_id": None,
        "route": {"available": True, "distance_m": 1000, "duration_s": 100, "steps": []},
    }]})

    registry.attach_city_artifact(
        "world_new", "city_a", v2_source, import_mode="city_expansion", city_version=2,
    )
    child = json.loads((
        world_root / "world_new" / "artifacts" / "routes" / "city_a" / "v2" / "route_cache.json"
    ).read_text(encoding="utf-8"))

    assert child["generation_statistics"]["external_api_requests"] == 0
    assert child["cache"][0]["origin_place_id"] == "a_new"
    assert child["cache"][0]["dest_place_id"] == "b"


def test_construction_attempt_survives_registry_refresh(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "outputs" / "mapseeds" / "city_a" / "base" / "world.json"
    _world(source, "city_a", "City A")

    monkeypatch.setattr(registry, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(registry, "WORLD_ROOT", tmp_path / "outputs" / "worlds")
    monkeypatch.setattr(registry, "MAPSEED_ROOT", tmp_path / "outputs" / "mapseeds")
    monkeypatch.setattr(registry, "ROUTE_ROOT", tmp_path / "outputs" / "routes")
    monkeypatch.setattr(registry, "CHARACTER_ROOT", tmp_path / "outputs" / "characters")
    monkeypatch.setattr(registry, "BENCHMARK_ROOT", tmp_path / "outputs" / "benchmarks")
    monkeypatch.setattr(registry, "CULTURE_ROOT", tmp_path / "outputs" / "culture")
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", tmp_path / "outputs" / "evidence")
    monkeypatch.setattr(registry, "DEFAULT_CITY_WORLD_SOURCES", {"city_a": "outputs/mapseeds/city_a/base/world.json"})

    registry.write_legacy_world_import("world_test")
    registry.record_construction_attempt(
        "world_test", "city_a", artifact_type="route_cache", status="completed", details={"api_requests": 1},
    )
    registry.write_legacy_world_import("world_test", overwrite=True)
    city = json.loads((tmp_path / "outputs" / "worlds" / "world_test" / "cities" / "city_a.json").read_text(encoding="utf-8"))

    assert city["construction_attempts"][0]["details"]["api_requests"] == 1


def test_archive_restore_and_detach_are_non_destructive(tmp_path: Path, monkeypatch, local_city_culture) -> None:
    source = tmp_path / "outputs" / "mapseeds" / "city_a" / "base" / "world.json"
    _world(source, "city_a", "City A")
    monkeypatch.setattr(registry, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(registry, "WORLD_ROOT", tmp_path / "outputs" / "worlds")
    monkeypatch.setattr(registry, "ROUTE_ROOT", tmp_path / "outputs" / "routes")
    monkeypatch.setattr(registry, "CHARACTER_ROOT", tmp_path / "outputs" / "characters")
    monkeypatch.setattr(registry, "CULTURE_ROOT", tmp_path / "outputs" / "culture")
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", tmp_path / "outputs" / "evidence")

    registry.create_empty_world("world_new")
    registry.attach_city_artifact("world_new", "city_a", source)
    assert registry.set_city_lifecycle_status("world_new", "city_a", "archived")["lifecycle_status"] == "archived"
    assert registry.set_city_lifecycle_status("world_new", "city_a", "active")["lifecycle_status"] == "active"
    detached = registry.detach_city_from_world("world_new", "city_a")
    manifest = json.loads((tmp_path / "outputs" / "worlds" / "world_new" / "world_manifest.json").read_text(encoding="utf-8"))

    assert detached["detached"] is True
    assert manifest["city_ids"] == []
    assert source.is_file()
    assert (tmp_path / "outputs" / "worlds" / "world_new" / "cities" / "city_a.json").is_file()


def test_service_detail_materialization_rejects_changed_active_version_before_fetch(
    tmp_path: Path, monkeypatch,
) -> None:
    v1 = tmp_path / "inputs" / "v1" / "world.json"
    v2 = tmp_path / "inputs" / "v2" / "world.json"
    _world(v1, "city_a", "City A")
    _world(v2, "city_a", "City A")
    world_root = tmp_path / "outputs" / "worlds"
    monkeypatch.setattr(registry, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(registry, "WORLD_ROOT", world_root)
    monkeypatch.setattr(registry, "ROUTE_ROOT", tmp_path / "outputs" / "routes")
    monkeypatch.setattr(registry, "CHARACTER_ROOT", tmp_path / "outputs" / "characters")
    monkeypatch.setattr(registry, "CULTURE_ROOT", tmp_path / "outputs" / "culture")
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", tmp_path / "outputs" / "evidence")

    registry.create_empty_world("world_test")
    registry.attach_city_artifact(
        "world_test", "city_a", v1, import_mode="legacy_import", city_version=1,
    )
    registry.attach_city_artifact(
        "world_test", "city_a", v2, import_mode="legacy_import", city_version=2,
    )
    monkeypatch.setattr(
        registry,
        "write_service_detail_snapshot",
        lambda *_args, **_kwargs: pytest.fail("website collection must not start"),
    )

    with pytest.raises(ValueError, match="active city version changed"):
        registry.materialize_city_service_detail_snapshots(
            "world_test", "city_a", expected_city_version=1,
        )
