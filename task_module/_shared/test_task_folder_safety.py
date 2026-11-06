"""Regression coverage for portable task-folder creation."""

from __future__ import annotations

from pathlib import Path

import pytest

from task_module._shared.base_generator import BaseTaskGenerator, make_folder_name
from shared.io import write_json
from task_module._shared.batch_state import incomplete_task_directories
from task_module._shared import output_writer


def test_folder_name_strips_windows_terminal_characters_and_illegal_symbols() -> None:
    assert make_folder_name(3, "I am trying to identify a place") == "task_0003_I am"
    assert make_folder_name(4, " . ") == "task_0004_task"
    assert make_folder_name(5, "a/b? ") == "task_0005_ab"
    assert make_folder_name(6, "CON  ") == "task_0006_task"

    for prompt in ("I am ", "Cafe. ", "a/b? ", "\x00bad"):
        folder = make_folder_name(9, prompt)
        assert not folder.endswith((" ", "."))
        assert not any(char in folder for char in '<>:"/\\|?*')


def test_output_writer_removes_a_half_written_task_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task_dir = tmp_path / "tasks" / make_folder_name(1, "I am ")

    def fail_write(*_args, **_kwargs) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr(output_writer, "write_json", fail_write)
    with pytest.raises(OSError, match="simulated write failure"):
        output_writer.write_task_folder(
            task_dir,
            {"task_id": "task_0001", "task_type": "place_grounding"},
            [], [], {},
        )

    assert not task_dir.exists()
    assert incomplete_task_directories(tmp_path) == []


def test_output_writer_can_atomically_write_the_previous_windows_failure_case(tmp_path: Path) -> None:
    task_dir = tmp_path / "tasks" / make_folder_name(3, "I am trying to identify a place")
    output_writer.write_task_folder(
        task_dir,
        {"task_id": "task_0003", "task_type": "place_grounding", "user_prompt": "I am trying to identify a place"},
        [], [], {},
    )

    assert task_dir.name == "task_0003_I am"
    assert (task_dir / "task.json").is_file()
    assert (task_dir / "world_slice.json").is_file()


def test_incomplete_task_directory_is_detected_before_an_append(tmp_path: Path) -> None:
    incomplete = tmp_path / "tasks" / "task_0003_I am"
    incomplete.mkdir(parents=True)

    assert incomplete_task_directories(tmp_path) == [incomplete]


@pytest.mark.parametrize("path", ["task_module/curated/exporter.py"])
def test_every_task_writer_uses_the_shared_folder_name_builder(path: str) -> None:
    assert "make_folder_name(" in Path(path).read_text(encoding="utf-8")


def _safety_generator(tmp_path: Path) -> BaseTaskGenerator:
    generator = BaseTaskGenerator(
        {"cities": [{"city_id": "demo_city"}]}, {"characters": []}, [], [], tmp_path, seed=123,
    )
    generator.task_type = "safety_planning"
    return generator


def _write_safety_identity(task_dir: Path, *, task_id: str, prompt: str) -> None:
    task_dir.mkdir(parents=True)
    write_json(task_dir / "task.json", {
        "task_id": task_id,
        "task_type": "safety_planning",
        "city_id": "demo_city",
        "user_prompt": prompt,
    })
    write_json(task_dir / "task_sandbox.json", {
        "character": {"character_id": "character_1"},
        "risky_target": {"place_id": "place_target"},
        "safety_risk_case": {
            "family": "traffic_access_risk",
            "variant": "pedestrian_access_advisory",
            "app": "CityNewsApp",
            "observation_contract": {"tool": "get_info", "arguments": {"district_id": "district_1"}},
        },
        "acceptable_plans": [{"place_id": "place_safe"}],
    })


def test_repair_rng_uses_distinct_but_reproducible_streams(tmp_path: Path) -> None:
    left = _safety_generator(tmp_path / "left")
    right = _safety_generator(tmp_path / "right")

    assert [left.next_generation_rng().randrange(1_000_000) for _ in range(2)] == [
        right.next_generation_rng().randrange(1_000_000) for _ in range(2)
    ]


def test_safety_semantic_duplicate_ignores_llm_wording_but_keeps_decision_identity(tmp_path: Path) -> None:
    generator = _safety_generator(tmp_path)
    accepted = tmp_path / "tasks" / "task_0001"
    candidate = tmp_path / "tasks" / "task_0004"
    _write_safety_identity(accepted, task_id="task_0001", prompt="请避开临时限制区域。")
    write_json(accepted / "task_validation.json", {"state": "formal", "valid": True})
    _write_safety_identity(candidate, task_id="task_0004", prompt="请根据当前风险调整行程。")

    assert generator._existing_semantic_duplicate(candidate) == "task_0001"
