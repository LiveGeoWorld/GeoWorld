"""Regression coverage for Windows-safe discovery session storage."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from world_module.map.config import MapGeneratorConfig
from world_module.map.generator import MapGenerator
from world_module.map.seed import CitySeed
import world_module.map.generator as generator_module
import world_module.map._internal.discovery.runner as runner_module
from world_module.map._internal.discovery.runner import (
    DiscoverySessionRunner,
    _hard_cap,
    _response_text,
)


def _seed() -> CitySeed:
    return CitySeed(
        city_id="london_smoke",
        city_name="London",
        country="United Kingdom",
        center={"lat": 51.5072, "lng": -0.1276},
        timezone="Europe/London",
    )


def test_discovery_uses_short_session_path_and_migrates_legacy_outline(tmp_path: Path, monkeypatch) -> None:
    # Keep the fixture itself short enough to create a legacy session on Windows.
    # The deep-path calculation is covered separately below.
    output_root = tmp_path / "generated_map"
    seed = _seed()
    legacy = output_root / seed.city_id / "intermediates" / "discovery_session"
    legacy.mkdir(parents=True)
    (legacy / "01_city_outline.json").write_text(json.dumps({"outline": "reusable"}), encoding="utf-8")

    seen: dict[str, Path] = {}

    class FakePipelineConfig:
        def stage_model(self, _stage: str) -> str:
            return "test_endpoint"

        def model_endpoint(self, _endpoint: str) -> dict[str, str]:
            return {"model": "test"}

    class FakeRunner:
        def __init__(self, **_kwargs) -> None:
            pass

        def run(self, _seed: CitySeed, session_dir: Path) -> dict:
            seen["session_dir"] = Path(session_dir)
            return {"ok": True}

    monkeypatch.setattr(generator_module, "DiscoverySessionRunner", FakeRunner)
    monkeypatch.setattr(MapGenerator, "_pipeline_config", lambda _self: FakePipelineConfig())
    monkeypatch.setattr(MapGenerator, "_config_path", lambda _self: "config.yaml")

    result = MapGenerator(MapGeneratorConfig(output_root=str(output_root))).discover(seed)
    expected = output_root / "_d" / hashlib.sha1(seed.city_id.encode("utf-8")).hexdigest()[:10]

    assert result == {"ok": True}
    assert seen["session_dir"] == expected
    assert (expected / "01_city_outline.json").is_file()
    assert (legacy / "01_city_outline.json").is_file()


def test_parallel_discovery_artifacts_use_short_stable_paths(tmp_path: Path) -> None:
    runner = DiscoverySessionRunner()
    runner.session_dir = (
        Path.cwd() / "outputs" / "worlds" / "world_001" / "artifacts" / "cities"
        / "london_smoke" / "expansions" / "exp_1784792417_7b5e54" / "generated_map" / "_d" / "4e28e05897"
    )
    path = runner._parallel_step_path("02_districts", 0)
    old_path = runner.session_dir / "02_districts" / "02_districts_station_business_core.json"

    assert path == runner.session_dir / "d" / "001.json"
    assert len(str(path)) < len(str(old_path)) - 25


def test_world_manager_structural_targets_are_exact() -> None:
    assert _hard_cap(10) == 10
    assert _hard_cap(12) == 12


def test_discovery_reads_openai_compatible_chat_content() -> None:
    response = {"choices": [{"message": {"content": "{\"ok\": true}"}}]}
    assert _response_text(response) == '{"ok": true}'


def _corridor(name: str) -> dict:
    return {
        "name": name,
        "corridor_class": "transport_corridor",
        "mode": "rail",
        "corridor_type": "rail_corridor",
        "served_areas": ["District A", "District B"],
        "node_names": ["Node A", "Node B", "Node C"],
        "corridor_service_bands": [],
    }


def test_structural_shortfall_is_automatically_repaired_before_downstream_stages(tmp_path: Path, monkeypatch) -> None:
    runner = DiscoverySessionRunner(structural_repair_batch=1)
    runner.session_dir = tmp_path / "job_a"
    calls: list[str] = []

    def fake_call(_prompt: str, *, request_name: str, **_kwargs) -> dict:
        calls.append(request_name)
        return {"corridor_candidates": [_corridor("New Corridor")]}

    monkeypatch.setattr(runner, "_call_json", fake_call)
    districts = [
        {"name": "District A", "role_hints": ["x"], "task_affordances": ["x"], "failure_modes": ["x"], "keywords": ["x"]},
        {"name": "District B", "role_hints": ["x"], "task_affordances": ["x"], "failure_modes": ["x"], "keywords": ["x"]},
    ]
    accepted_districts, district_audit = runner_module._collect_structural_candidates(
        [{"district_candidates": districts}], "district_candidates", runner_module.district_validation_reason, 2,
    )
    accepted_corridors, corridor_audit = runner_module._collect_structural_candidates(
        [{"corridor_candidates": [_corridor("Existing Corridor")] }], "corridor_candidates",
        runner_module.corridor_validation_reason, 2,
    )
    result = runner._repair_structural_shortfalls(
        _seed(), {}, {"city_summary": {}}, [{"district_candidates": districts}],
        [{"corridor_candidates": [_corridor("Existing Corridor")] }],
        accepted_districts, accepted_corridors, district_audit, corridor_audit, 2, 2,
    )

    assert len(result[0]) == 2
    assert len(result[1]) == 2
    assert result[2]["remaining_gaps"] == {}
    assert calls == ["corridors_round_1"]
    assert (tmp_path / "job_a" / "structural_repairs" / "batch_01" / "report.json").is_file()


def test_structural_repair_preserves_reasoned_shortfall_after_two_rounds(tmp_path: Path, monkeypatch) -> None:
    runner = DiscoverySessionRunner(structural_repair_batch=1)
    runner.session_dir = tmp_path / "job_b"
    monkeypatch.setattr(runner, "_call_json", lambda *_args, **_kwargs: {"corridor_candidates": [{"name": "invalid"}]})
    districts = [
        {"name": "District A", "role_hints": ["x"], "task_affordances": ["x"], "failure_modes": ["x"], "keywords": ["x"]},
        {"name": "District B", "role_hints": ["x"], "task_affordances": ["x"], "failure_modes": ["x"], "keywords": ["x"]},
    ]
    accepted_districts, district_audit = runner_module._collect_structural_candidates(
        [{"district_candidates": districts}], "district_candidates", runner_module.district_validation_reason, 2,
    )
    accepted_corridors, corridor_audit = runner_module._collect_structural_candidates(
        [{"corridor_candidates": [_corridor("Existing Corridor")] }], "corridor_candidates",
        runner_module.corridor_validation_reason, 2,
    )
    _districts, corridors, report = runner._repair_structural_shortfalls(
        _seed(), {}, {"city_summary": {}}, [{"district_candidates": districts}],
        [{"corridor_candidates": [_corridor("Existing Corridor")] }],
        accepted_districts, accepted_corridors, district_audit, corridor_audit, 2, 2,
    )

    assert len(corridors) == 1
    assert report["remaining_gaps"] == {"corridors": (1, 2)}
    assert len(report["rounds"]) == 2
    assert report["corridors"]["audit"]["invalid_by_reason"]["insufficient_node_names"] == 2


def test_discovery_uses_job_session_identifier_when_provided(tmp_path: Path, monkeypatch) -> None:
    output_root = tmp_path / "generated_map"
    seen: dict[str, Path] = {}

    class FakePipelineConfig:
        def stage_model(self, _stage: str) -> str:
            return "test_endpoint"

        def model_endpoint(self, _endpoint: str) -> dict[str, str]:
            return {"model": "test"}

    class FakeRunner:
        def __init__(self, **_kwargs) -> None:
            pass

        def run(self, _seed: CitySeed, session_dir: Path) -> dict:
            seen["session_dir"] = Path(session_dir)
            return {"ok": True}

    monkeypatch.setattr(generator_module, "DiscoverySessionRunner", FakeRunner)
    monkeypatch.setattr(MapGenerator, "_pipeline_config", lambda _self: FakePipelineConfig())
    monkeypatch.setattr(MapGenerator, "_config_path", lambda _self: "config.yaml")
    MapGenerator(MapGeneratorConfig(output_root=str(output_root), discovery_session_id="job_a1b2c3d4")).discover(_seed())

    assert seen["session_dir"] == output_root / "_d" / "job_a1b2c3d4"
