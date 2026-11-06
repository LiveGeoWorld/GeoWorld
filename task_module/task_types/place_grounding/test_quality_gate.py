from __future__ import annotations

from task_module.task_types.place_grounding.batch_generation import quality_gate


def test_prompt_quality_check_preserves_evaluation_contract(monkeypatch) -> None:
    received = {}

    def fake_validate(payload, **_kwargs):
        received.update(payload)
        return []

    monkeypatch.setattr(quality_gate, "validate_prompt_payload", fake_validate)

    result = quality_gate._prompt_quality_check({
        "prompt_language": "en",
        "user_prompt": "A sufficiently detailed grounding request for a real place.",
        "review_translation": {"language": "zh-CN", "user_prompt": "地点识别请求"},
        "visual_clues": ["street frontage"],
        "text_clues": ["nearby landmark"],
        "negative_clues": [],
        "candidate_scope": {"city_id": "cape_town_south_africa"},
        "evaluation": {"must_mention": ["candidate"], "must_avoid": []},
        "prompt_evidence_map": {"street frontage": ["image_assets[0]"]},
        "evidence_ledger": {},
        "review_language": "zh-CN",
    })

    assert result["passed"] is True
    assert received["evaluation"] == {
        "must_mention": ["candidate"],
        "must_avoid": [],
    }


def test_named_anchor_recall_requires_every_structured_poi() -> None:
    world = {
        "cities": [{"city_id": "city", "name": "City"}],
        "places": [{
            "place_id": "place-1",
            "name": "Canonical Place",
            "city_id": "city",
            "district_id": "district",
            "address": "1 Main Street",
            "location": {"lat": 1.0, "lng": 2.0},
            "primary_type": "store",
            "map_projection": {"aliases": ["Canonical Place English"]},
        }],
    }
    example = {
        "candidate_scope": {"city_id": "city"},
        "gold": {
            "anchor_places": ["Canonical Place English", "Missing Nearby POI"],
        },
    }

    result = quality_gate._mapapp_named_anchor_recall_check(example, world)

    assert result["passed"] is False
    assert result["severity"] == "hard"
    assert result["message"] == "unsearchable named anchors: Missing Nearby POI"


def test_named_anchor_recall_accepts_frozen_same_place_alias() -> None:
    world = {
        "cities": [{"city_id": "city", "name": "City"}],
        "places": [{
            "place_id": "place-1",
            "name": "Canonical Place",
            "city_id": "city",
            "district_id": "district",
            "address": "1 Main Street",
            "location": {"lat": 1.0, "lng": 2.0},
            "primary_type": "store",
            "map_projection": {"aliases": ["Canonical Place English"]},
        }],
    }
    example = {
        "candidate_scope": {"city_id": "city"},
        "gold": {"anchor_places": ["Canonical Place English"]},
    }

    result = quality_gate._mapapp_named_anchor_recall_check(example, world)

    assert result == {
        "name": "mapapp_named_anchor_recall",
        "passed": True,
        "severity": "hard",
        "message": "ok",
    }
