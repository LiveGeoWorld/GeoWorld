from __future__ import annotations

import json
from pathlib import Path

from task_module._shared.benchmark_layout import batch_dir, iter_batches, latest_batch, task_dirs
from task_module._shared import task_assets


def _batch(root: Path, task_type: str, world: str, city: str, batch: str) -> Path:
    folder = batch_dir(root, task_type, world, city, batch) / "tasks" / "task_0001"
    folder.mkdir(parents=True)
    (folder / "task.json").write_text(json.dumps({"task_id": "task_0001"}), encoding="utf-8")
    return folder.parent.parent


def test_layout_keeps_worlds_as_a_real_path_boundary(tmp_path: Path) -> None:
    _batch(tmp_path, "multi_entity", "world_001", "shanghai_smoke", "old")
    current = _batch(tmp_path, "multi_entity", "world_002", "shanghai_smoke", "current")

    found = list(iter_batches(tmp_path, task_type="multi_entity", world_id="world_002", city_scope="shanghai_smoke"))

    assert found == [current]
    assert latest_batch(tmp_path, "multi_entity", "world_002", "shanghai_smoke") == current
    assert len(task_dirs(current)) == 1


def test_latest_batch_uses_recorded_time_when_windows_has_no_latest_link(tmp_path: Path) -> None:
    older = _batch(tmp_path, "route_planning", "world_002", "shanghai_smoke", "zzz_legacy_name")
    newer = _batch(tmp_path, "route_planning", "world_002", "shanghai_smoke", "aaa_current_name")
    (older / "batch_metadata.json").write_text(
        json.dumps({"created_at": "2026-07-24T09:00:00+00:00"}), encoding="utf-8"
    )
    (newer / "batch_metadata.json").write_text(
        json.dumps({"created_at": "2026-07-24T10:00:00+00:00"}), encoding="utf-8"
    )

    assert latest_batch(tmp_path, "route_planning", "world_002", "shanghai_smoke") == newer


def test_streetview_assets_are_copied_into_the_task_bundle(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "workspace"
    source = root / "outputs" / "benchmarks" / "place_grounding" / "batch_assets" / "streetview" / "view.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"street-view")
    task_dir = root / "outputs" / "benchmarks" / "place_grounding" / "world_002" / "city" / "batch" / "tasks" / "task_0001"
    task_dir.mkdir(parents=True)
    (task_dir / "task_spec.json").write_text(json.dumps({"image_path": str(source.relative_to(root))}), encoding="utf-8")
    monkeypatch.setattr(task_assets, "_workspace_root", lambda: root)

    result = task_assets.localize_task_assets(task_dir)

    assert result["localized"] == 1
    assert (task_dir / "assets" / "streetview" / "view.jpg").read_bytes() == b"street-view"
    assert json.loads((task_dir / "task_spec.json").read_text(encoding="utf-8"))["image_path"] == "assets/streetview/view.jpg"
