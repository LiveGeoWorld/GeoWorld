"""Regression coverage for local-only legacy task formalization."""

from __future__ import annotations

from pathlib import Path

from shared.io import write_json
from task_module._shared.formalization import is_formal_task, make_legacy_stamp, validate_legacy_task


def _legacy_grounding(root: Path, *, local_asset: bool = True) -> tuple[Path, Path]:
    batch = root / "place_grounding" / "world_002" / "demo_city" / "batch_001"
    task_dir = batch / "tasks" / "task_0001"
    task_dir.mkdir(parents=True)
    write_json(task_dir / "task.json", {
        "schema_version": "geoword.task.v1", "task_id": "task_0001",
        "task_type": "place_grounding", "city_id": "demo_city", "user_prompt": "Identify this place.",
        "time_context": {"instruction_time": "2026-01-01T09:00:00+00:00"},
        "summary": {"gold_summary": {"place_id": "place_1"}, "input_assets": {"image_path": "assets/view.jpg"}},
    })
    write_json(task_dir / "task_sandbox.json", {"task_type": "place_grounding", "task_id": "task_0001"})
    write_json(task_dir / "world_slice.json", {})
    write_json(task_dir / "env_events.json", {"env_events": []})
    write_json(task_dir / "judge_sequence.json", {"stages": []})
    if local_asset:
        asset = task_dir / "assets" / "view.jpg"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"jpeg")
    write_json(batch / "world_provenance.json", {
        "world_id": "world_002", "city_id": "demo_city", "task_type": "place_grounding",
        "required_city_versions": {"demo_city": 1},
    })
    return batch, task_dir


def test_legacy_formalization_stamps_a_valid_task_without_changing_payload(tmp_path: Path) -> None:
    batch, task_dir = _legacy_grounding(tmp_path)
    before = (task_dir / "task.json").read_bytes()
    report = validate_legacy_task(task_dir, batch_dir=batch, task_type="place_grounding",
                                  world_id="world_002", city_id="demo_city")

    assert report["valid"] is True
    write_json(task_dir / "task_validation.json", make_legacy_stamp(report))
    assert is_formal_task(task_dir, "place_grounding") is True
    assert (task_dir / "task.json").read_bytes() == before


def test_invalid_legacy_task_is_never_default_formal_input(tmp_path: Path) -> None:
    batch, task_dir = _legacy_grounding(tmp_path, local_asset=False)
    report = validate_legacy_task(task_dir, batch_dir=batch, task_type="place_grounding",
                                  world_id="world_002", city_id="demo_city")

    assert report["valid"] is False
    assert "place_grounding image evidence is not localized in the task bundle" in report["errors"]
    write_json(task_dir / "task_validation.json", make_legacy_stamp(report, state="legacy-unverified"))
    assert is_formal_task(task_dir, "place_grounding") is False


def test_batch_marked_ineligible_excludes_every_task_even_if_its_stamp_is_formal(tmp_path: Path) -> None:
    batch, task_dir = _legacy_grounding(tmp_path)
    report = validate_legacy_task(task_dir, batch_dir=batch, task_type="place_grounding",
                                  world_id="world_002", city_id="demo_city")
    write_json(task_dir / "task_validation.json", make_legacy_stamp(report))
    write_json(batch / "batch_metadata.json", {"evaluation_eligibility": "ineligible"})

    assert is_formal_task(task_dir, "place_grounding") is False
