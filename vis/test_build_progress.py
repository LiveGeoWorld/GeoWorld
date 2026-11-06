"""Regression coverage for structured World Studio build progress."""

from __future__ import annotations

import json
from pathlib import Path

from vis import server
from world_module import registry
from world_module.map._internal.discovery.runner import _request_policy
from world_module.map._internal.quality_gate import _is_task_only_information_warning
from world_module.character.config import CharacterGeneratorConfig
from shared.llm_client import OpenAICompatibleChatClient


def _job() -> dict:
    return {
        "id": "job_progress_test",
        "status": "running",
        "seed": "seeds/example.json",
        "world_id": "world_001",
        "city_id": "example_city",
        "started_at": 1.0,
        "finished_at": None,
        "output": [],
        "registration": {},
        "phase": "map_generation",
    }


def test_generator_progress_event_updates_stage_and_is_not_console_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "WORLD_JOB_LOG_ROOT", tmp_path / "world_jobs")
    job = _job()

    consumed = server._consume_progress_event(
        job,
        '[geoword-progress] {"stage":"map_discovery","step":"02_districts","state":"running","label":"\\ufffd\\ufffd","completed":2,"total":5}',
    )

    assert consumed is True
    assert job["progress"]["current"]["stage"] == "map_discovery"
    assert job["progress"]["current"]["label"] == "区域组"
    assert job["progress"]["current"]["completed"] == 2
    assert job["progress"]["current"]["total"] == 5
    assert job["output"] == []
    assert (tmp_path / "world_jobs" / "job_progress_test.json").is_file()


def test_persisted_job_response_keeps_terminal_progress(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "WORLD_JOB_LOG_ROOT", tmp_path / "world_jobs")
    job = _job()
    job["status"] = "failed"
    job["finished_at"] = 5.0
    server._update_job_progress(job, "map_discovery", "failed", label="城市骨架", error="LLM 响应读取超时")

    restored = server._load_persisted_job(job["id"])
    response = server._job_response(restored)

    assert response["status"] == "failed"
    assert response["progress"]["current"]["label"] == "城市发现失败"
    assert response["progress"]["current"]["error"] == "LLM 响应读取超时"
    assert response["failure_detail"] == ""


def test_structural_shortfall_marker_is_not_reported_as_generic_process_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "WORLD_JOB_LOG_ROOT", tmp_path / "world_jobs")
    monkeypatch.setattr(server, "PIPELINE_ROOT", tmp_path)
    report = {
        "max_automatic_rounds": 2,
        "remaining_gaps": {"corridors": [14, 16]},
        "rounds": [{"round": 1}, {"round": 2}],
    }
    payload = {"report_path": "outputs/_d/job_a/shortfall.json", "report": report}
    parsed = server._structural_shortfall_from_output([
        "ordinary output",
        "[geoword-structural-shortfall] " + json.dumps(payload),
    ])
    assert parsed == payload

    job = _job()
    job.update({"structural_shortfall": payload, "structural_repair_report": report})
    plan = server._resume_plan(job)
    assert plan["stage"] == "map_generation"
    assert plan["reused"] == ["城市骨架", "区域", "通道"]
    assert server._job_response(job)["structural_repair_report"] == report


def test_world_studio_uses_stage_panel_and_shared_place_type_catalog() -> None:
    root = Path(server.__file__).parent
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    frontend = (root / "static" / "world-manager.js").read_text(encoding="utf-8")

    assert 'id="mgr-place-types"' in html
    assert 'data-type="doctor"' not in html
    assert 'id="mgr-gen-progress"' in html
    assert 'id="mgr-btn-cancel-job"' in html
    assert 'class="mgr-btn mgr-btn-secondary"' in html
    assert "renderGenerationProgress(job)" in frontend
    assert 'retryControls.classList.add("hidden")' not in frontend
    assert "/api/jobs/latest" in frontend
    assert "/cancel" in frontend
    assert "cancelled" in frontend
    assert "retry_mode: retryMode" in frontend
    assert "继续构建" in frontend
    assert "重新构建" in frontend
    assert "mgr-gen-recovery-plan" in html
    assert 'id="mgr-btn-register-actual"' in html
    assert "registerActualCandidate" in frontend
    assert "awaiting_registration_decision" in frontend
    assert "/api/place-type-catalog" in frontend
    assert "place_categories: placeCategories" in frontend
    assert "queryTypesForCategories(placeCategories)" in frontend
    assert 'return "";' in frontend
    assert "状态：待补齐" in frontend
    assert '<span data-ui-text>质量与资产</span>' in frontend
    english = json.loads((root.parent / "shared" / "web_ui" / "en.json").read_text(encoding="utf-8"))
    assert english["质量与资产"] == "Quality and assets"
    assert "产物完备度" not in frontend
    assert "质量通过" not in frontend


def test_cancelled_build_never_attaches_partial_artifacts(tmp_path: Path, monkeypatch) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.terminated = False

        def poll(self):
            return None if not self.terminated else 1

        def terminate(self) -> None:
            self.terminated = True

    monkeypatch.setattr(server, "WORLD_JOB_LOG_ROOT", tmp_path / "world_jobs")
    job = _job()
    process = FakeProcess()
    monkeypatch.setitem(server.JOBS, job["id"], job)
    monkeypatch.setitem(server.JOB_PROCESSES, job["id"], {"map": process})

    requested = server._cancel_world_job(job["id"])
    assert requested["status"] == "cancelling"
    assert process.terminated is True

    server._finalize_cancelled_world_job(job)
    persisted = server._load_persisted_job(job["id"])
    assert persisted["status"] == "cancelled"
    assert persisted["registration"]["status"] == "not_attached"
    assert persisted["registration"]["reason"] == "cancelled_by_user"


def test_dynamic_replanning_signal_is_not_a_map_quality_warning() -> None:
    assert _is_task_only_information_warning("too much information is visible at start") is True
    assert _is_task_only_information_warning("social_post count is low for a live city feed") is False


def test_failed_character_stage_marks_downstream_work_not_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "WORLD_JOB_LOG_ROOT", tmp_path / "world_jobs")
    job = _job()
    server._update_job_progress(job, "character_generation", "failed", label="人物关系网络失败")
    server._mark_stages_not_run(job, "cultural_knowledge", "world_registration")

    stages = {item["id"]: item for item in job["progress"]["stages"]}
    assert stages["character_generation"]["status"] == "failed"
    assert stages["cultural_knowledge"]["status"] == "not_run"
    assert stages["world_registration"]["status"] == "not_run"


def test_unavailable_cultural_assets_block_world_registration(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "WORLD_JOB_LOG_ROOT", tmp_path / "world_jobs")
    job = _job()
    server._fail_for_cultural_assets(job, {
        "status": "unavailable", "reason": "wikivoyage_page_without_eligible_evidence",
        "evidence_cards": 0,
    })

    stages = {item["id"]: item for item in job["progress"]["stages"]}
    assert job["status"] == "failed"
    assert "wikivoyage_page_without_eligible_evidence" in job["error"]
    assert stages["cultural_knowledge"]["status"] == "failed"
    assert stages["world_registration"]["status"] == "not_run"


def test_target_shortfall_waits_for_explicit_registration_decision(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "WORLD_JOB_LOG_ROOT", tmp_path / "world_jobs")
    job = _job()
    report = {
        "actual_counts": {"districts": 1, "corridors": 1, "places": 1, "characters": 2},
        "target_gaps": {"characters": 1}, "hard_errors": [], "repairable_gaps": [],
        "can_register_actual": True,
    }
    server._set_candidate_gate_decision(job, report)
    assert job["status"] == "awaiting_registration_decision"
    assert job["registration"]["status"] == "pending_user_confirmation"
    assert job["gate_report"] == report
    assert server._job_response(job)["gate_report"]["target_gaps"] == {"characters": 1}


def test_shortfall_resumes_from_candidate_snapshot_not_character_stage(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "PIPELINE_ROOT", tmp_path)
    map_path = tmp_path / "candidate" / "world.json"
    characters_path = tmp_path / "candidate" / "characters.json"
    map_path.parent.mkdir(parents=True)
    map_path.write_text("{}", encoding="utf-8")
    characters_path.write_text('{"characters": []}', encoding="utf-8")
    job = _job()
    job.update({
        "target_gaps": {"places": 19, "characters": 4},
        "checkpoints": {
            "map_world": {"stage": "map_quality", **server._artifact_checkpoint(map_path)},
            "characters": {"stage": "character_generation", **server._artifact_checkpoint(characters_path)},
        },
    })

    plan = server._resume_plan(job)
    assert plan["stage"] == "candidate_repair"
    assert plan["reused"] == ["候选地图", "候选人物"]
    assert plan["target_gaps"] == {"places": 19, "characters": 4}


def test_automatic_candidate_repairs_poi_and_character_gaps_without_restart(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "WORLD_JOB_LOG_ROOT", tmp_path / "world_jobs")
    world_path = tmp_path / "candidate" / "world.json"
    characters_path = tmp_path / "candidate" / "characters.json"
    world_path.parent.mkdir(parents=True)
    world_path.write_text(json.dumps({
        "districts": [{"district_id": "district_a"}],
        "corridors": [{"corridor_id": "corridor_a"}],
        "places": [{"place_id": "place_a", "name": "Existing", "map_projection": {"google_place_id": "old"}}],
        "services": [], "information_items": [], "event_templates": [], "actors": [],
    }), encoding="utf-8")
    characters_path.write_text(json.dumps({"characters": [{"character_id": "char_a", "name": "Existing"}], "relationships": []}), encoding="utf-8")
    job = _job()
    job.update({
        "requested_targets": {"districts": 1, "corridors": 1, "places": 2, "characters": 2},
        "target_city_version": 2,
        "output": [],
    })
    calls: list[str] = []

    def fake_poi_repair(repair_job, candidate_path, attempt):
        calls.append("poi")
        payload = json.loads(candidate_path.read_text(encoding="utf-8"))
        payload["places"].append({"place_id": "place_b", "name": "New", "map_projection": {"google_place_id": "new"}})
        candidate_path.write_text(json.dumps(payload), encoding="utf-8")
        return {"attempted": True, "repaired": True}

    def fake_character_repair(repair_job, candidate_path, chars_path, *, max_attempts):
        calls.append("character")
        payload = json.loads(chars_path.read_text(encoding="utf-8"))
        payload["characters"].append({"character_id": "char_b", "name": "New"})
        chars_path.write_text(json.dumps(payload), encoding="utf-8")
        return {"attempted": True, "repaired": True}

    monkeypatch.setattr(server, "_run_poi_gap_repair", fake_poi_repair)
    monkeypatch.setattr(server, "_run_character_gap_repair", fake_character_repair)

    counts, gaps = server._run_automatic_candidate_repairs(job, world_path, characters_path)

    assert calls == ["poi", "character"]
    assert counts == {"districts": 1, "corridors": 1, "places": 2, "characters": 2}
    assert gaps == {}
    assert job["automatic_repair_state"]["status"] == "completed"


def test_poi_supplement_uses_nested_source_ids_and_keeps_candidate_structure() -> None:
    candidate = {
        "districts": [{"district_id": "district_a"}],
        "corridors": [{"corridor_id": "corridor_a"}],
        "places": [{"place_id": "place_old", "map_projection": {"google_place_id": "old"}}],
        "services": [],
    }
    supplement = {
        "districts": [{"district_id": "district_new"}],
        "corridors": [{"corridor_id": "corridor_new"}],
        "places": [
            {"place_id": "duplicate", "map_projection": {"google_place_id": "old"}},
            {"place_id": "place_new", "map_projection": {"google_place_id": "new"}},
        ],
        "services": [
            {"service_id": "service_old", "place_id": "duplicate"},
            {"service_id": "service_new", "place_id": "place_new"},
        ],
    }

    merged, metadata = server._append_poi_supplement(candidate, supplement, "repair_1", 2)

    assert [item["place_id"] for item in merged["places"]] == ["place_old", "place_new"]
    assert merged["districts"] == candidate["districts"]
    assert merged["corridors"] == candidate["corridors"]
    assert merged["services"][0]["place_id"] == "place_new"
    assert metadata == {"accepted_places": 1, "rejected_duplicate_places": 1, "accepted_services": 1}


def test_poi_gap_repair_writes_exclusion_seed_and_appends_only_new_assets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(server, "WORLD_JOB_LOG_ROOT", tmp_path / "world_jobs")
    candidate_path = tmp_path / "candidate" / "world.json"
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_text(json.dumps({
        "districts": [{"district_id": "district_a"}], "corridors": [],
        "places": [{"place_id": "place_old", "map_projection": {"google_place_id": "old"}}],
        "services": [], "information_items": [], "event_templates": [], "actors": [],
    }), encoding="utf-8")
    (tmp_path / "map").mkdir()
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps({
        "city_id": "city_a", "city_name": "City A", "country": "Test",
        "center": {"lat": 1.0, "lng": 2.0}, "timezone": "UTC",
    }), encoding="utf-8")
    repair_id = "poi_repair_job_progress_test_1"
    supplement_path = tmp_path / "map" / "city_a" / repair_id / "world.json"
    supplement_path.parent.mkdir(parents=True)
    supplement_path.write_text(json.dumps({
        "places": [{"place_id": "place_new", "map_projection": {"google_place_id": "new"}}],
        "services": [{"service_id": "service_new", "place_id": "place_new"}],
    }), encoding="utf-8")
    (supplement_path.parent / "map_manifest.json").write_text(
        json.dumps({"operation_ledger": {"external_services": {"requests": 1}}}), encoding="utf-8",
    )

    class FakeProcess:
        stdout = []
        returncode = 0

        def wait(self):
            return 0

    monkeypatch.setattr(server.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    job = _job()
    job.update({
        "run_seed": "seed.json", "map_output_root": "map", "geo_provider": "google",
        "target_city_version": 2, "target_gaps": {"places": 1},
        "candidate_counts": {"places": 1}, "requested_targets": {"places": 2},
        "output": [],
    })

    result = server._run_poi_gap_repair(job, candidate_path, 1)

    repair_seed = json.loads((candidate_path.parent / "_poi_gap_repairs" / job["id"] / "attempt_1" / "poi_repair_seed.json").read_text(encoding="utf-8"))
    updated = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert result["repaired"] is True
    assert repair_seed["output_variant"] == repair_id
    assert repair_seed["quality_profile"] == "poi_supplement"
    assert "district_count" not in repair_seed
    assert "corridor_count" not in repair_seed
    assert repair_seed["excluded_place_ids"] == ["old"]
    assert [item["place_id"] for item in updated["places"]] == ["place_old", "place_new"]
    assert updated["services"][0]["place_id"] == "place_new"
    assert job["automatic_repairs"][0]["operation_ledger"]["external_services"]["requests"] == 1


def test_legacy_expansion_floor_failure_reuses_only_exact_batch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "PIPELINE_ROOT", tmp_path)
    batch_path = tmp_path / "map" / "city_a" / "base" / "world.json"
    batch_path.parent.mkdir(parents=True)
    batch_path.write_text(json.dumps({
        "cities": [{"city_id": "city_a"}],
        "districts": [{}, {}], "corridors": [{}, {}, {}, {}],
        "places": [{}, {}, {}],
    }), encoding="utf-8")
    (batch_path.parent / "quality_report.json").write_text(json.dumps({
        "counts": {"districts": 2, "corridors": 4, "places": 3},
        "errors": ["Only 2 districts; expected at least 3"],
    }), encoding="utf-8")
    job = _job()
    job.update({
        "mode": "incremental_expansion", "city_id": "city_a", "map_output_root": "map",
        "expansion_plan": {"delta": {"districts": 2, "corridors": 4, "places": 3}},
    })

    plan = server._resume_plan(job)

    assert plan["stage"] == "map_scope_recovery"
    assert plan["map_path"] == batch_path
    job["expansion_plan"]["delta"]["places"] = 4
    assert server._reusable_legacy_expansion_batch(job) is None


def test_historical_target_shortfall_receives_read_only_decision_view(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "PIPELINE_ROOT", tmp_path)
    map_path = tmp_path / "candidate" / "world.json"
    characters_path = tmp_path / "candidate" / "characters.json"
    map_path.parent.mkdir(parents=True)
    map_path.write_text('{"districts":[],"corridors":[],"places":[]}', encoding="utf-8")
    characters_path.write_text('{"characters":[{"character_id":"char_1"}],"relationships":[]}', encoding="utf-8")
    job = _job()
    job.update({
        "status": "target_not_reached",
        "map_output_root": "candidate/generated_map",
        "requested_targets": {"districts": 0, "corridors": 0, "places": 0, "characters": 2},
        "checkpoints": {
            "characters": {"stage": "character_generation", **server._artifact_checkpoint(characters_path)},
        },
    })

    response = server._job_response(job)
    assert response["status"] == "awaiting_registration_decision"
    assert response["gate_report"]["target_gaps"] == {"characters": 1}
    assert job["status"] == "target_not_reached"


def test_user_confirmed_actual_registration_records_version_metadata(tmp_path: Path, monkeypatch, local_city_culture) -> None:
    map_path = tmp_path / "candidate" / "world.json"
    characters_path = tmp_path / "candidate" / "characters.json"
    seed_path = tmp_path / "seeds" / "city.json"
    map_path.parent.mkdir(parents=True)
    map_path.write_text(json.dumps({
        "cities": [{"city_id": "city_a", "name": "City A", "timezone": "UTC"}],
        "districts": [], "corridors": [], "places": [], "services": [],
        "information_items": [], "event_templates": [], "actors": [],
    }), encoding="utf-8")
    characters_path.write_text(json.dumps({"city_id": "city_a", "characters": [
        {"character_id": "char_1", "name": "One"},
    ], "relationships": []}), encoding="utf-8")
    seed_path.parent.mkdir()
    seed_path.write_text(json.dumps({"city_name": "City A"}), encoding="utf-8")

    monkeypatch.setattr(server, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(server, "WORLD_JOB_LOG_ROOT", tmp_path / "job_logs")
    monkeypatch.setattr(registry, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(registry, "WORLD_ROOT", tmp_path / "outputs" / "worlds")
    monkeypatch.setattr(registry, "ROUTE_ROOT", tmp_path / "outputs" / "routes")
    monkeypatch.setattr(registry, "CHARACTER_ROOT", tmp_path / "outputs" / "characters")
    monkeypatch.setattr(registry, "CULTURE_ROOT", tmp_path / "outputs" / "culture")
    monkeypatch.setattr(registry, "EVIDENCE_ROOT", tmp_path / "outputs" / "evidence")
    monkeypatch.setattr(server, "ensure_cultural_assets", lambda *_, **__: {
        "status": "available", "evidence_cards": 1,
        "evidence_path": "outputs/evidence/city_a.json",
        "constraint_path": "outputs/culture/city_a.json",
    })
    registry.create_empty_world("world_test")

    class ImmediateThread:
        def __init__(self, *, target, daemon=False):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(server.threading, "Thread", ImmediateThread)
    previous = _job()
    previous.update({
        "id": "job_source", "status": "awaiting_registration_decision", "world_id": "world_test",
        "city_id": "city_a", "seed": "seeds/city.json", "run_seed": "seeds/city.json",
        "geo_provider": "auto", "map_output_root": "candidate", "character_output_root": "candidate",
        "requested_targets": {"districts": 0, "corridors": 0, "places": 0, "characters": 2},
        "target_city_version": 1, "expansion_id": "", "expansion_city_id": "", "mode": "new_city",
        "progress": server._new_job_progress(),
        "checkpoints": {
            "map_world": {"stage": "map_quality", **server._artifact_checkpoint(map_path)},
            "characters": {"stage": "character_generation", **server._artifact_checkpoint(characters_path)},
        },
    })
    source_id = server._start_actual_registration(previous)
    result = server.JOBS[source_id]
    city = json.loads((registry.WORLD_ROOT / "world_test" / "cities" / "city_a.json").read_text(encoding="utf-8"))

    assert result["status"] == "completed"
    assert result["registration"]["status"] == "attached_with_shortfall"
    assert result["registration"]["target_gaps"] == {"characters": 1}
    assert city["city_versions"][0]["registration_metadata"]["mode"] == "user_confirmed_actual"
    server.JOBS.pop(source_id, None)


def test_resume_plan_prefers_relationship_checkpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "PIPELINE_ROOT", tmp_path)
    map_path = tmp_path / "candidate" / "world.json"
    draft_path = tmp_path / "candidate" / "characters_draft.json"
    map_path.parent.mkdir(parents=True)
    map_path.write_text("{}", encoding="utf-8")
    draft_path.write_text('{"city_id":"example_city","characters":[]}', encoding="utf-8")
    job = _job()
    job["status"] = "failed"
    job["progress"] = server._new_job_progress()
    job["progress"]["current"] = {"stage": "character_generation"}
    job["checkpoints"] = {
        "map_world": {"stage": "map_quality", **server._artifact_checkpoint(map_path)},
        "characters_draft": {"stage": "character_generation", **server._artifact_checkpoint(draft_path)},
    }

    plan = server._resume_plan(job)
    assert plan["stage"] == "relationships"
    assert plan["reused"] == ["地图", "人物档案"]
    assert json.loads(json.dumps(server._job_response(job)))["resume_plan"]["stage"] == "relationships"


def test_resume_plan_does_not_reuse_a_map_that_failed_batch_quality(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "PIPELINE_ROOT", tmp_path)
    map_path = tmp_path / "candidate" / "world.json"
    map_path.parent.mkdir(parents=True)
    map_path.write_text("{}", encoding="utf-8")
    (map_path.parent / "quality_report.json").write_text(
        json.dumps({"ok": False, "errors": ["initial_city produced 43 places; expected exactly 266"]}),
        encoding="utf-8",
    )
    job = _job()
    job.update({
        "status": "failed",
        "batch_quality": {"ok": False},
        "checkpoints": {"map_world": {"stage": "map_quality", **server._artifact_checkpoint(map_path)}},
    })

    plan = server._resume_plan(job)

    assert plan["stage"] == "map_generation"
    assert plan["reused"] == ["城市骨架", "区域", "通道"]


def test_initial_city_map_resume_accepts_null_expansion_plan(monkeypatch) -> None:
    previous = _job()
    previous.update({
        "id": "job_failed_initial", "seed": "seeds/city.json", "geo_provider": "auto",
        "world_id": "world_test", "expansion_city_id": "", "expansion_plan": None,
        "requested_targets": {"districts": 2, "corridors": 2, "places": 10, "characters": 2},
    })
    monkeypatch.setattr(server, "_resume_plan", lambda _job: {
        "stage": "map_generation", "label": "retry map", "reused": [],
    })
    received = {}
    monkeypatch.setattr(server, "_start_generation", lambda *args, **kwargs: received.update({"args": args, "kwargs": kwargs}) or "job_new")

    assert server._start_resumed_generation(previous) == "job_new"
    assert received["args"][4] == ""
    assert received["args"][5] is None
    assert received["args"][6] == previous["requested_targets"]


def test_character_timeout_budgets_cover_relationship_network() -> None:
    cfg = CharacterGeneratorConfig()
    assert cfg.llm_timeout_seconds == 300
    assert cfg.relationship_timeout_seconds == 600
    assert OpenAICompatibleChatClient(base_url="https://example.test/v1", api_key="", model="test").timeout_seconds == 300


def test_discovery_policy_keeps_web_search_quality_and_long_request_budget() -> None:
    outline = _request_policy(
        "city_outline", timeout_seconds=300, web_search_timeout_seconds=600,
        max_attempts=2, use_web_search=True,
    )
    groups = _request_policy(
        "02_districts", timeout_seconds=300, web_search_timeout_seconds=600,
        max_attempts=2, use_web_search=True,
    )
    plain = _request_policy(
        "03_corridor_groups", timeout_seconds=300, web_search_timeout_seconds=600,
        max_attempts=2, use_web_search=False,
    )

    assert [attempt["timeout_seconds"] for attempt in outline] == [600, 600]
    assert [attempt["timeout_seconds"] for attempt in groups] == [600, 600]
    assert [attempt["timeout_seconds"] for attempt in plain] == [300, 300]
    assert [attempt["backoff_seconds"] for attempt in groups] == [5, 0]
