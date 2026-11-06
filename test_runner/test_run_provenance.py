from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from test_runner.runner import _resolve_place_world, _resolve_place_world_snapshot, _resolve_route_cache, _task_batch_provenance, run_all_tasks
from test_runner.place.session import _load_mapapp_world
from test_runner.schema import AgentRunResult


def test_task_batch_provenance_reads_explicit_world_and_versions(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tmp_path / "world_provenance.json").write_text(json.dumps({
        "world_id": "world_002",
        "required_city_versions": {"shanghai_smoke": 2, "tokyo_complex": "1"},
    }), encoding="utf-8")

    provenance = _task_batch_provenance(tasks_dir)

    assert provenance["world_id"] == "world_002"
    assert provenance["required_city_versions"] == {"shanghai_smoke": 2, "tokyo_complex": 1}
    assert provenance["path"].endswith("world_provenance.json")


def test_task_batch_provenance_is_empty_for_legacy_batch(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    assert _task_batch_provenance(tasks_dir) == {
        "path": "", "world_id": "", "required_city_versions": {},
    }


def test_managed_batch_never_falls_back_to_global_route_cache(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "outputs" / "benchmarks" / "route_planning" / "city_a" / "batch_001" / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir.parent / "world_provenance.json").write_text(json.dumps({
        "world_id": "world_002", "required_city_versions": {"city_a": 2},
    }), encoding="utf-8")
    global_cache = tmp_path / "outputs" / "routes" / "city_a" / "route_cache.json"
    global_cache.parent.mkdir(parents=True)
    global_cache.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert _resolve_route_cache("", tasks_dir) == ""


def test_managed_batch_reads_matching_versioned_route_cache(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "outputs" / "benchmarks" / "route_planning" / "city_a" / "batch_001" / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir.parent / "world_provenance.json").write_text(json.dumps({
        "world_id": "world_002", "required_city_versions": {"city_a": 2},
    }), encoding="utf-8")
    cache = tmp_path / "outputs" / "worlds" / "world_002" / "artifacts" / "routes" / "city_a" / "v2" / "route_cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert _resolve_route_cache("", tasks_dir).replace("\\", "/").endswith(
        "outputs/worlds/world_002/artifacts/routes/city_a/v2/route_cache.json"
    )


def test_place_task_uses_its_provenance_artifact_not_global_world(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    task_dir = tmp_path / "outputs" / "benchmarks" / "place_grounding" / "world_002" / "city_a" / "batch" / "tasks" / "task_0001"
    task_dir.mkdir(parents=True)
    artifact = tmp_path / "outputs" / "worlds" / "world_002" / "artifacts" / "city_a_v2.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({"cities": [{"city_id": "city_a"}]}), encoding="utf-8")
    import hashlib
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    registry = tmp_path / "outputs" / "worlds" / "world_002" / "cities" / "city_a.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"city_versions": [{"version": 2, "artifacts": {"map_world": {
        "path": str(artifact.relative_to(tmp_path)).replace("\\", "/"), "sha256": digest,
    }}}]}), encoding="utf-8")
    (task_dir / "task_provenance.json").write_text(json.dumps({
        "world_id": "world_002", "city_id": "city_a",
        "generation_context": {"city_version": "v2", "artifact_snapshot": {"map_world": digest}},
    }), encoding="utf-8")

    resolved = _resolve_place_world(str(tmp_path / "wrong_global.json"), task_dir, {"city_id": "city_a"})

    assert Path(resolved).resolve() == artifact.resolve()


def test_place_session_never_merges_other_local_worlds(tmp_path: Path) -> None:
    bound = tmp_path / "bound.json"
    bound.write_text(json.dumps({"cities": [{"city_id": "london_smoke"}], "places": []}), encoding="utf-8")

    world = _load_mapapp_world(bound)

    assert world["cities"] == [{"city_id": "london_smoke"}]
    assert world["_source_world_path"] == str(bound.resolve())


def test_place_task_builds_a_complete_frozen_world_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    task_dir = tmp_path / "outputs" / "benchmarks" / "place_grounding" / "world_002" / "city_a" / "batch" / "tasks" / "task_0001"
    task_dir.mkdir(parents=True)
    import hashlib
    snapshot_entries = {}
    for city_id in ("city_a", "city_b"):
        artifact = tmp_path / "outputs" / "worlds" / "world_002" / "artifacts" / f"{city_id}_v1.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps({
            "cities": [{"city_id": city_id}],
            "places": [{"place_id": f"place_{city_id}", "city_id": city_id}],
        }), encoding="utf-8")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        registry = tmp_path / "outputs" / "worlds" / "world_002" / "cities" / f"{city_id}.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps({"city_versions": [{"version": 1, "artifacts": {"map_world": {
            "path": str(artifact.relative_to(tmp_path)).replace("\\", "/"), "sha256": digest,
        }}}]}), encoding="utf-8")
        snapshot_entries[city_id] = {"version": 1, "path": str(artifact.relative_to(tmp_path)), "sha256": digest}
    (task_dir / "task_provenance.json").write_text(json.dumps({
        "world_id": "world_002", "city_id": "city_a",
        "generation_context": {
            "city_version": "v1", "world_city_versions": {"city_a": 1, "city_b": 1},
            "world_map_snapshot": snapshot_entries,
        },
    }), encoding="utf-8")

    binding = _resolve_place_world_snapshot("", task_dir, {"city_id": "city_a"})
    world = _load_mapapp_world(binding)

    assert binding["city_versions"] == {"city_a": 1, "city_b": 1}
    assert {city["city_id"] for city in world["cities"]} == {"city_a", "city_b"}
    assert {place["place_id"] for place in world["places"]} == {"place_city_a", "place_city_b"}


def test_runner_uses_only_explicit_formal_task_sample(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "outputs" / "benchmarks" / "multi_entity" / "world_002" / "city_a" / "batch" / "tasks"
    first = tasks_dir / "task_0001"
    second = tasks_dir / "task_0002"
    for folder in (first, second):
        folder.mkdir(parents=True)
        (folder / "task.json").write_text(json.dumps({
            "task_id": folder.name, "task_type": "multi_entity",
        }), encoding="utf-8")
    (tasks_dir.parent / "world_provenance.json").write_text(json.dumps({
        "world_id": "world_002", "required_city_versions": {"city_a": 1},
    }), encoding="utf-8")

    def fake_run(folder: Path, **_kwargs) -> AgentRunResult:
        result = AgentRunResult(task_id=folder.name, user_prompt="", gt_route_id="")
        result.task_type = "multi_entity"
        result.tokens_used = {"prompt": 1, "completion": 1, "total": 2}
        return result

    with patch("test_runner.runner.is_formal_task", return_value=True), \
         patch("test_runner.runner.run_task", side_effect=fake_run):
        results, _ = run_all_tasks(
            tasks_dir, selected_task_folders=[second],
            output_path=str(tmp_path / "evaluation" / "results.json"),
        )

    assert [result.task_id for result in results] == ["task_0002"]


def test_runner_creates_result_layout_before_the_first_task_runs(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "outputs" / "benchmarks" / "multi_entity" / "world_002" / "city_a" / "batch" / "tasks"
    task = tasks_dir / "task_0001"
    task.mkdir(parents=True)
    (task / "task.json").write_text(json.dumps({
        "task_id": "task_0001", "task_type": "multi_entity",
    }), encoding="utf-8")
    (tasks_dir.parent / "world_provenance.json").write_text(json.dumps({
        "world_id": "world_002", "required_city_versions": {"city_a": 1},
    }), encoding="utf-8")
    output_path = tmp_path / "evaluation" / "results.json"

    def fake_run(folder: Path, **_kwargs) -> AgentRunResult:
        assert (output_path.parent / "tasks").is_dir()
        result = AgentRunResult(task_id=folder.name, user_prompt="", gt_route_id="")
        result.task_type = "multi_entity"
        result.tokens_used = {"prompt": 1, "completion": 1, "total": 2}
        return result

    with patch("test_runner.runner.is_formal_task", return_value=True), \
         patch("test_runner.runner.run_task", side_effect=fake_run):
        run_all_tasks(tasks_dir, output_path=str(output_path))

    assert (output_path.parent / "tasks" / "0001.json").is_file()
    index = json.loads((output_path.parent / "tasks" / "task_index.json").read_text(encoding="utf-8"))
    assert index["entries"] == [{"task_id": "task_0001", "task_type": "multi_entity", "file_name": "0001.json"}]


def test_place_task_does_not_receive_a_route_cache_override(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "outputs" / "benchmarks" / "place_grounding" / "world_002" / "city_a" / "batch" / "tasks"
    task = tasks_dir / "task_0001"
    task.mkdir(parents=True)
    (task / "task.json").write_text(json.dumps({
        "task_id": "task_0001", "task_type": "place_grounding",
    }), encoding="utf-8")
    (tasks_dir.parent / "world_provenance.json").write_text(json.dumps({
        "world_id": "world_002", "required_city_versions": {"city_a": 1},
    }), encoding="utf-8")
    stale_cache = tmp_path / "outputs" / "routes" / "other_city" / "route_cache.json"
    stale_cache.parent.mkdir(parents=True)
    stale_cache.write_text("{}", encoding="utf-8")

    def fake_run(folder: Path, **kwargs) -> AgentRunResult:
        assert kwargs["route_cache_path"] == ""
        result = AgentRunResult(task_id=folder.name, user_prompt="", gt_route_id="")
        result.task_type = "place_grounding"
        result.tokens_used = {"prompt": 1, "completion": 1, "total": 2}
        return result

    with patch("test_runner.runner.is_formal_task", return_value=True), \
         patch("test_runner.runner.run_task", side_effect=fake_run):
        run_all_tasks(tasks_dir, route_cache_path=str(stale_cache), output_path=str(tmp_path / "evaluation" / "results.json"))


def test_short_result_artifact_survives_a_windows_length_limited_output_root(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "benchmarks" / "multi_entity" / "world_002" / "city_a" / "batch" / "tasks"
    task = tasks_dir / "task_0001"
    task.mkdir(parents=True)
    (task / "task.json").write_text(json.dumps({
        "task_id": "task_shanghai_smoke_multi_entity_0001", "task_type": "multi_entity",
    }), encoding="utf-8")
    (tasks_dir.parent / "world_provenance.json").write_text(json.dumps({
        "world_id": "world_002", "required_city_versions": {"city_a": 1},
    }), encoding="utf-8")
    run_root = tmp_path / "evaluation"
    while len(str(run_root / "tasks")) < 200:
        run_root = run_root / "evaluation_segment"
    output_path = run_root / "results.json"
    short_temp_path = output_path.parent / "tasks" / ".0001.json.12345.random.tmp"
    legacy_temp_path = output_path.parent / "tasks" / (
        ".multi_entity__task_shanghai_smoke_multi_entity_0001.12345.random.tmp"
    )
    assert len(str(short_temp_path)) < 260
    assert len(str(legacy_temp_path)) > 260

    def fake_run(folder: Path, **_kwargs) -> AgentRunResult:
        result = AgentRunResult(task_id="task_shanghai_smoke_multi_entity_0001", user_prompt="", gt_route_id="")
        result.task_type = "multi_entity"
        result.tokens_used = {"prompt": 1, "completion": 1, "total": 2}
        return result

    with patch("test_runner.runner.is_formal_task", return_value=True), \
         patch("test_runner.runner.run_task", side_effect=fake_run):
        run_all_tasks(tasks_dir, output_path=str(output_path))

    assert (output_path.parent / "tasks" / "0001.json").is_file()
