"""Compile narrowly-scoped, source-backed Region rules for one formal batch.

The compiler never promotes a raw Wikivoyage paragraph verbatim. It emits a
structured candidate only when an explicit text pattern, an exact source quote,
and current-world applicability all agree. Candidates are batch-local
``pending_task_audit`` material and never mutate world assets.
"""

from __future__ import annotations

import hashlib
from typing import Any


_TEMPLATES = (
    {"needle": "use only your right hand", "type": "etiquette",
     "keywords": ["dining", "right hand"], "scenario": "与当地朋友共餐",
     "hidden_rule": {"forbidden_actions": ["eat_with_left_hand"],
                     "decision_effect": {"dimension": "protocol_actions", "activity": "shared_meal",
                                         "required_action_ids": ["eat_with_right_hand"]}}},
    {"needle": "stick your chopsticks vertically", "type": "etiquette",
     "keywords": ["dining", "chopsticks"], "scenario": "与当地朋友共餐",
     "hidden_rule": {"forbidden_actions": ["stick_chopsticks_vertically_in_rice"],
                     "decision_effect": {"dimension": "protocol_actions", "activity": "shared_meal",
                                         "required_action_ids": ["place_chopsticks_horizontally"]}}},
    {"needle": "remove your shoes before you enter", "type": "place_of_worship_etiquette",
     "keywords": ["place of worship", "shoes"], "scenario": "首次参观宗教场所",
     "hidden_rule": {"forbidden_actions": ["enter_place_of_worship_without_removing_shoes"],
                     "required_place_types": ["mosque", "hindu_temple", "temple"],
                     "decision_effect": {"dimension": "protocol_actions", "activity": "worship_visit",
                                         "required_action_ids": ["remove_shoes_before_entry"]}}},
    {"needle": "do not point at the statues of deities with your index finger", "type": "place_of_worship_etiquette",
     "keywords": ["temple", "respect"], "scenario": "参观当地寺庙",
     "hidden_rule": {"forbidden_actions": ["point_at_deity_with_index_finger"],
                     "required_place_types": ["hindu_temple", "temple", "buddhist_temple"],
                     "decision_effect": {"dimension": "protocol_actions", "activity": "worship_visit",
                                         "required_action_ids": ["avoid_pointing_at_deities"]}}},
    {"needle": "public display of affection is still frowned upon", "type": "public_etiquette",
     "keywords": ["public", "etiquette"], "scenario": "公共区域的短途游览",
     "hidden_rule": {"forbidden_actions": ["public_display_of_affection"],
                     "decision_effect": {"dimension": "protocol_actions", "activity": "public_outing",
                                         "required_action_ids": ["avoid_public_display_of_affection"]}}},
    {"needle": "smoking on the street is strictly prohibited", "type": "public_regulation",
     "keywords": ["street", "smoking"], "scenario": "步行游览期间的休息安排",
     "hidden_rule": {"forbidden_actions": ["smoke_outside_designated_area"],
                     "decision_effect": {"dimension": "protocol_actions", "activity": "public_outing",
                                         "required_action_ids": ["use_designated_smoking_area_only"]}}},
)


def compile_structured_candidates(kb: Any, world: dict[str, Any]) -> list[dict[str, Any]]:
    """Return deterministic candidates supported by the supplied local evidence."""
    primary_types = {str(place.get("primary_type") or "") for place in world.get("places", [])
                     if isinstance(place, dict)}
    candidates: list[dict[str, Any]] = []
    for card in (kb.load_evidence() or {}).get("evidence_cards", []):
        if not isinstance(card, dict):
            continue
        text = str(card.get("text") or "")
        lowered = text.lower()
        for template in _TEMPLATES:
            needle = template["needle"]
            if needle not in lowered:
                continue
            hidden = dict(template["hidden_rule"])
            required = set(hidden.get("required_place_types") or [])
            if required and not required.intersection(primary_types):
                continue
            start = lowered.index(needle)
            quote = text[max(0, start - 72): min(len(text), start + len(needle) + 96)].strip()
            evidence_id = str(card.get("evidence_id") or "")
            digest = hashlib.sha256(f"{kb.city_id}|{evidence_id}|{needle}".encode("utf-8")).hexdigest()[:12]
            candidates.append({
                "id": f"region_rule_{digest}", "type": template["type"],
                "rule_text": "Avoid: " + ", ".join(str(value).replace("_", " ")
                                                      for value in hidden["forbidden_actions"]),
                "keywords": list(template["keywords"]), "scenario": template["scenario"],
                "time": {"mode": "year_round"}, "severity": "hard",
                "review": "pending_task_audit", "source_type": "wikivoyage_structured_batch_candidate",
                "candidate_origin": "deterministic_source_pattern_v1", "evidence_ids": [evidence_id],
                "source_quote": quote,
                "source_section": str(card.get("section") or ""),
                "source_title": "Wikivoyage",
                "hidden_rule": hidden,
                # Regional knowledge remains private construction/Judge data.
                # The tested Agent receives neither a guide App nor this label.
                "verifiable_by": ["source_backed_private_region_judge"],
            })
    return candidates
