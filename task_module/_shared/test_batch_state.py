"""Regression checks for safe append semantics in the construction phase."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from task_module._shared.batch_state import (
    append_seed,
    attempt_seed,
    batch_seed,
    ensure_batch_seed,
    existing_gold_ids,
    existing_task_count,
    next_task_index,
    record_generation_run,
    task_seed,
)
from task_module.generator import _remaining_task_count


def test_append_state_uses_monotonic_indices_and_gold_reservations() -> None:
    root = Path("virtual_batch")
    task_dirs = [root / "tasks" / "task_0001_first", root / "tasks" / "task_0003_third"]
    specs = {
        str(task_dirs[0] / "task_spec.json"): {"gold": {"place_id": "poi_a"}},
        str(task_dirs[1] / "task_spec.json"): {"gold": {"place_id": "poi_b"}},
    }

    with patch("task_module._shared.batch_state.task_directories", return_value=task_dirs), \
         patch.object(Path, "is_file", return_value=True), \
         patch("task_module._shared.batch_state.read_json", side_effect=lambda path: specs[str(path)]):
        assert existing_task_count(root) == 2
        assert next_task_index(root) == 4
        assert existing_gold_ids(root, "place_id") == {"poi_a", "poi_b"}


def test_withdrawn_dynamic_task_keeps_its_commit_number_reserved(tmp_path) -> None:
    (tmp_path / "tasks" / "task_0003_kept").mkdir(parents=True)
    commits = tmp_path / "generation_log" / "episode_commits"
    commits.mkdir(parents=True)
    old = commits / "task_0007_task_.json"
    old.write_text('{"status": "committed"}')
    (commits / "task_invalid_record.json").write_text('{}')
    assert next_task_index(tmp_path) == 8
    assert existing_task_count(tmp_path) == 1
    assert old.read_text() == '{"status": "committed"}'


def test_lower_commit_number_does_not_reduce_next_task_index(tmp_path) -> None:
    (tmp_path / "tasks" / "task_0009_kept").mkdir(parents=True)
    commits = tmp_path / "generation_log" / "episode_commits"
    commits.mkdir(parents=True)
    (commits / "task_0002_task_.json").write_text('{"status": "committed"}')
    assert next_task_index(tmp_path) == 10


def test_batch_task_and_attempt_seed_lineage_is_stable_and_isolated() -> None:
    root = Path("virtual_batch")
    stored = {"schema_version": "test", "batch_seed": 987654321}
    with patch.object(Path, "is_file", return_value=True), \
         patch("task_module._shared.batch_state.read_json", return_value=stored):
        assert ensure_batch_seed(root) == 987654321
        assert batch_seed(123, root) == 987654321
        assert append_seed(123, root) == 987654321

    first = task_seed(987654321, "world_002", "city", "region_aware", 1)
    second = task_seed(987654321, "world_002", "city", "region_aware", 2)
    assert first != second
    assert first == task_seed(987654321, "world_002", "city", "region_aware", 1)
    assert attempt_seed(first, 1) != attempt_seed(first, 2)
    assert attempt_seed(first, 1) == attempt_seed(first, 1)


def test_new_batch_seed_is_generated_once_and_persisted() -> None:
    root = Path("virtual_new_batch")
    writes = []
    with patch.object(Path, "is_file", return_value=False), \
         patch("task_module._shared.batch_state.write_json", side_effect=lambda path, value: writes.append((path, value))):
        value = ensure_batch_seed(root)
    assert value > 0
    assert writes[-1][1]["batch_seed"] == value


def test_discovery_gold_reservation_reads_ranked_top1_service() -> None:
    root = Path("virtual_discovery_batch")
    task_dir = root / "tasks" / "task_0001_discovery"
    spec = {
        "gold": {
            "candidates": [
                {"service_id": "service_second", "rank": 2},
                {"service_id": "service_top1", "rank": 1},
            ],
        },
    }
    with patch("task_module._shared.batch_state.task_directories", return_value=[task_dir]), \
         patch.object(Path, "is_file", return_value=True), \
         patch("task_module._shared.batch_state.read_json", return_value=spec):
        assert existing_gold_ids(root, "service_id") == {"service_top1"}


def test_append_run_history_is_preserved() -> None:
    root = Path("virtual_batch")
    writes: list[list[dict]] = []
    with patch.object(Path, "is_file", return_value=False), \
         patch("task_module._shared.batch_state.write_json", side_effect=lambda _path, value: writes.append(value)):
        record_generation_run(root, {"generated_task_ids": ["task_0001"]})
    with patch.object(Path, "is_file", return_value=True), \
         patch("task_module._shared.batch_state.read_json", return_value=writes[0]), \
         patch("task_module._shared.batch_state.write_json", side_effect=lambda _path, value: writes.append(value)):
        record_generation_run(root, {"generated_task_ids": ["task_0002"]})

    assert [entry["generated_task_ids"] for entry in writes[-1]] == [["task_0001"], ["task_0002"]]
    assert all(entry["recorded_at"] for entry in writes[-1])


def test_reuse_count_targets_the_final_batch_total() -> None:
    assert _remaining_task_count(5, 0) == 5
    assert _remaining_task_count(5, 4) == 1
    assert _remaining_task_count(5, 5) == 0
    assert _remaining_task_count(5, 9) == 0
