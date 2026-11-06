from __future__ import annotations

from pathlib import Path

import pytest

from test_runner.agent.llm_agent import LLMAgent
from app.runtime.environment import Environment
from test_runner.runner import _task_input_images


def _write_config(path: Path, *, supports_vision: bool) -> None:
    path.write_text(
        "model_endpoints:\n"
        "  test_model:\n"
        "    provider: openai_compatible\n"
        "    model: test-model\n"
        "    base_url: https://example.invalid/v1\n"
        "    api_key: test\n"
        f"    supports_vision: {'true' if supports_vision else 'false'}\n",
        encoding="utf-8",
    )


def test_text_only_initial_message_retains_legacy_string_shape() -> None:
    agent = object.__new__(LLMAgent)
    agent.user_prompt = "text-only task"
    agent._input_images = []

    assert agent._initial_user_content() == "text-only task"


def test_image_input_is_encoded_without_a_filesystem_path(tmp_path: Path) -> None:
    image = tmp_path / "scene.jpg"
    image.write_bytes(b"not-a-real-jpeg-but-a-valid-transport-payload")
    agent = object.__new__(LLMAgent)
    agent.user_prompt = "identify this place"
    agent._input_images = LLMAgent._prepare_input_images([image])

    content = agent._initial_user_content()

    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "identify this place"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert str(image) not in str(content)


def test_responses_serialization_converts_image_to_input_image() -> None:
    payload = LLMAgent._responses_messages([
        {"role": "user", "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc"}},
        ]},
    ])

    assert payload == [{"role": "user", "content": [
        {"type": "input_text", "text": "look at this"},
        {"type": "input_image", "image_url": "data:image/jpeg;base64,abc"},
    ]}]


def test_task_loader_uses_only_declared_evaluation_image(tmp_path: Path) -> None:
    task_dir = tmp_path / "task_0001"
    streetview = task_dir / "assets" / "streetview"
    streetview.mkdir(parents=True)
    evaluation = streetview / "view_h0.jpg"
    reference = streetview / "view_h90.jpg"
    evaluation.write_bytes(b"evaluation")
    reference.write_bytes(b"authoring-reference")
    task_data = {"summary": {"input_assets": {
        "image_path": "assets/streetview/view_h0.jpg",
        "prompt_reference_image_path": "assets/streetview/view_h90.jpg",
    }}}

    assert _task_input_images(task_dir, task_data) == [evaluation.resolve()]


def test_task_loader_rejects_an_image_path_outside_the_task_folder(tmp_path: Path) -> None:
    task_dir = tmp_path / "task_0001"
    task_dir.mkdir()
    with pytest.raises(ValueError, match="escapes"):
        _task_input_images(task_dir, {"summary": {"input_assets": {
            "image_path": "../outside.jpg",
        }}})


def test_image_task_requires_a_declared_vision_endpoint(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    image = tmp_path / "scene.jpg"
    image.write_bytes(b"image")
    _write_config(config, supports_vision=False)

    with pytest.raises(ValueError, match="supports_vision"):
        LLMAgent(
            Environment(), "identify this", model_key="test_model", config_yaml=str(config),
            input_images=[image],
        )
