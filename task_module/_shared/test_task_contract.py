"""Focused in-memory tests for canonical task-folder normalization."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from task_module._shared import task_contract


class TaskContractTests(unittest.TestCase):
    def _canonicalize(self, task_type: str, *, prompt: str = "Plan this task.",
                      prompt_language: str = "", nearby_anchors: list[str] | None = None,
                      world_slice_names: list[str] | None = None) -> tuple[dict, dict, dict]:
        task_dir = Path("virtual_task")
        store = {
            str(task_dir / "task.json"): {
                "task_id": "task_0001", "task_type": task_type,
                "city_id": "shanghai", "user_prompt": prompt,
                "prompt_language": prompt_language,
                "summary": {"prompt_language": prompt_language, "candidate_scope": {
                    "nearby_anchors": nearby_anchors or [],
                }},
            },
            str(task_dir / "task_sandbox.json"): {"task_type": task_type},
            str(task_dir / "world_slice.json"): {"places": [
                {"name": name} for name in world_slice_names or []
            ]},
            str(task_dir / "env_events.json"): {"env_events": [{
                "event_id": "legacy",
                "app": "WeatherApp",
                "scope": {"type": "district", "id": "district_001"},
                "state_patch": {"condition": "heavy_rain"},
                "activation": {"mode": "on_start"},
            }]},
            str(task_dir / "judge_sequence.json"): {"stages": []},
        }

        def read(path: Path) -> dict:
            return deepcopy(store[str(path)])

        def write(path: Path, payload: dict) -> None:
            store[str(path)] = deepcopy(payload)

        with patch.object(task_contract, "read_json", side_effect=read), \
             patch.object(task_contract, "write_json", side_effect=write), \
             patch.object(task_contract.Path, "exists", side_effect=lambda: True):
            report = task_contract.canonicalize_task_folder(task_dir, task_type, "shanghai", "Asia/Shanghai")
        return report, store[str(task_dir / "task.json")], store[str(task_dir / "env_events.json")]

    def test_static_task_gets_time_context_and_empty_overlay(self) -> None:
        report, task, events = self._canonicalize("itinerary")
        self.assertTrue(report["valid"])
        self.assertIn("instruction_time", task["time_context"])
        self.assertEqual(events["env_events"], [])

    def test_dynamic_task_rejects_legacy_event_envelope(self) -> None:
        report, _, events = self._canonicalize("dynamic_replanning")
        self.assertFalse(report["valid"])
        self.assertEqual(events["env_events"][0]["event_id"], "legacy")
        self.assertIn(
            "dynamic env_events.json must use geoword.dynamic_events.v2",
            report["errors"],
        )

    def test_finalization_warns_for_mixed_language_without_rejection(self) -> None:
        report, _, _ = self._canonicalize(
            "itinerary",
            prompt="Please find a 餐厅 near the station.",
            prompt_language="en",
        )

        self.assertTrue(report["valid"])
        self.assertTrue(report["warnings"])

    def test_chinese_prompt_allows_source_backed_english_anchor_name(self) -> None:
        report, _, _ = self._canonicalize(
            "itinerary",
            prompt="请帮我确认 Oriental Riverside Hotel 附近的酒店。",
            prompt_language="zh-CN",
            nearby_anchors=["Oriental Riverside Hotel"],
        )

        self.assertTrue(report["valid"])

    def test_chinese_prompt_allows_normal_english_place_type_words(self) -> None:
        report, _, _ = self._canonicalize(
            "itinerary",
            prompt="请找一家 restaurant，附近最好有 park，也可以是 coffee shop。",
            prompt_language="zh-CN",
        )

        self.assertTrue(report["valid"])

    def test_chinese_prompt_rejects_snake_case_place_type_enum(self) -> None:
        report, _, _ = self._canonicalize(
            "itinerary",
            prompt="请找一家 coffee_shop。",
            prompt_language="zh-CN",
        )

        self.assertFalse(report["valid"])
        self.assertIn("Task user_prompt contains a raw English place-type enum", report["errors"])

    def test_chinese_prompt_allows_world_slice_proper_name_containing_a_type_word(self) -> None:
        report, _, _ = self._canonicalize(
            "itinerary",
            prompt="请先前往 InterContinental Shanghai Pudong Hotel underground garage。",
            prompt_language="zh-CN",
            world_slice_names=["InterContinental Shanghai Pudong Hotel underground garage"],
        )

        self.assertTrue(report["valid"])

    def test_legacy_writer_gets_explicit_language_before_formalization(self) -> None:
        report, task, _ = self._canonicalize(
            "itinerary",
            prompt="请为我安排一份从浦东出发的一日行程。",
        )

        self.assertTrue(report["valid"])
        self.assertEqual(task["prompt_language"], "zh-CN")
        self.assertEqual(task["summary"]["prompt_language"], "zh-CN")

    def test_dominant_english_warns_for_a_chinese_generic_type(self) -> None:
        report, task, _ = self._canonicalize(
            "itinerary",
            prompt="Please find a 餐厅 near the station.",
        )

        self.assertEqual(task["prompt_language"], "en")
        self.assertTrue(report["valid"])
        self.assertTrue(report["warnings"])

    def test_english_prompt_with_chinese_person_name_is_formal(self) -> None:
        report, task, _ = self._canonicalize(
            "itinerary", prompt="I'm 陈慧玲, 68, visiting Singapore.", prompt_language="en")
        self.assertTrue(report["valid"])
        self.assertEqual(report["errors"], [])
        self.assertIn("陈慧玲", task["user_prompt"])
        self.assertTrue(report["warnings"])

    def test_english_internal_enum_still_rejected(self) -> None:
        report, _, _ = self._canonicalize(
            "itinerary", prompt="Find a coffee_shop.", prompt_language="en")
        self.assertFalse(report["valid"])

    def test_multi_entity_requires_a_feasible_private_plan(self) -> None:
        report, _, _ = self._canonicalize("multi_entity")

        self.assertFalse(report["valid"])
        self.assertIn(
            "multi_entity feasible-only task requires expected_feasible=true and at least one acceptable plan",
            report["errors"],
        )


if __name__ == "__main__":
    unittest.main()
