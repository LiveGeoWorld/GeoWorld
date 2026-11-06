from copy import deepcopy

import pytest

from task_module.task_types.place_grounding.batch_generation.config import BatchGenerationConfig
from task_module.task_types.place_grounding.batch_generation import llm_writer as writer


def evidence():
    return {"place_evidence": {"name": "Target", "primary_type": "cafe",
            "nearby_anchors": [{"name": "上海博物馆"}, {"name": "アトレ上野"}],
            "confusable_names": ["其他店"]}}


def test_runtime_loads_english_not_preserved_chinese():
    prompt = writer.load_system_prompt()
    assert prompt.startswith("You are the GEOBench")
    assert "2-4 sentences" in prompt
    assert "I only remember..." in prompt
    assert "foot traffic and density of shops" in prompt
    assert "Confusable candidates are for authoring comparison" in prompt
    assert "`negative_clues` may be empty" not in prompt
    assert all(f"{number}. " in prompt for number in range(1, 12))
    original = writer.system_prompt_path().with_name("system.original.zh.md").read_text(encoding="utf-8")
    assert original.startswith("你是 GEOBench")
    assert "东京任务用自然日语表达" in original
    assert "Japanese for Tokyo" not in prompt


def payload():
    return {"prompt_language": "en", "user_prompt":
            "Please identify this place in the street photo, near 上海博物馆, using map evidence.",
            "visual_clues": ["A street frontage"], "text_clues": ["Near 上海博物馆"],
            "negative_clues": [], "grounding_constraints": ["Search the map"],
            "candidate_scope": {}, "evaluation": {"must_mention": ["上海博物馆"]},
            "review_translation": {"language": "zh-CN", "user_prompt": "请辨认地点"},
            "prompt_evidence_map": {"anchor": ["place_evidence.nearby_anchors"]}}


@pytest.mark.parametrize("language", ["auto", "zh-CN", "ja-JP", "en"])
def test_legacy_config_generates_english(language):
    config = BatchGenerationConfig(prompt_language=language)
    assert config.language_for_city("tokyo_corridor_smoke") == "en"
    assert config.language_for_city("shanghai_smoke") == "en"


def test_exact_frozen_names_and_review_translation_allowed():
    assert writer.validate_prompt_payload(payload(), evidence=evidence(), expected_language="en") == []
    assert not writer._has_untranslated_cjk("Near アトレ上野.", evidence())


@pytest.mark.parametrize("field", ["user_prompt", "visual_clues", "text_clues", "negative_clues", "grounding_constraints", "candidate_scope", "evaluation"])
def test_untranslated_prose_rejected(field):
    value = deepcopy(payload())
    value[field] = "Please find the 咖啡店 in this street photograph using the available map clues."
    assert any("untranslated CJK" in issue for issue in writer.validate_prompt_payload(value, evidence=evidence()))


def test_no_partial_or_invented_alias_exemption():
    assert writer._has_untranslated_cjk("Near 上海博物馆旁边.", evidence())
    assert writer._has_untranslated_cjk("Near 上博.", evidence())


def test_gold_leakage_still_rejected():
    facts = evidence()
    facts["place_evidence"]["name"] = "上海博物馆"
    assert any("leaks forbidden" in issue for issue in writer.validate_prompt_payload(payload(), evidence=facts))


def test_four_sentence_prompt_not_rejected_by_character_count():
    value = payload()
    value["user_prompt"] = (
        "A friend sent me this photograph while walking through a busy shopping area and asked me to help identify the place. "
        "I remember a cafe near 上海博物馆, but I cannot recall its name or tell which storefront my friend meant. "
        "Please compare the visible surroundings with the nearby places returned by the map rather than relying on a single sign. "
        "Which location best fits the photograph and these clues, and what evidence supports your choice?"
    )
    assert len(value["user_prompt"]) > 360
    assert writer.validate_prompt_payload(value, evidence=evidence(), expected_language="en") == []


def test_fallback_ignores_old_language():
    result = writer.deterministic_write_grounding_prompt(
        evidence=evidence(), quality_profile={"ambiguity_score": 4},
        granularity="exact_poi", prompt_language="ja-JP")
    assert result["prompt_language"] == "en"
    assert "cafe" in result["user_prompt"]
    assert "上海博物馆" in result["user_prompt"]
    assert result["review_translation"]["language"] == "zh-CN"


def test_llm_call_and_failure_fallback_use_english(monkeypatch):
    from task_module.task_types.place_grounding.batch_generation import pipeline
    from types import SimpleNamespace
    seen = []

    def fail(**kwargs):
        seen.append(kwargs["prompt_language"])
        raise RuntimeError("simulated service failure")

    monkeypatch.setattr(pipeline, "write_grounding_prompt_with_llm", fail)
    result = pipeline._write_prompt_payload(
        config=BatchGenerationConfig(prompt_language="auto"), evidence=evidence(),
        quality_profile={"ambiguity_score": 4},
        target=SimpleNamespace(city_id="tokyo_corridor_smoke", granularity="exact_poi"))
    assert seen == ["en"]
    assert result["prompt_language"] == "en"
    assert result["generation_method"]["type"] == "deterministic_fallback"
