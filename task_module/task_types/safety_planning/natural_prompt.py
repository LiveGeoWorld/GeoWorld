"""Writer contract for a fixed safety-planning choice."""

from __future__ import annotations

import json
from typing import Any

from task_module.task_types.safety_planning.choice_pipeline import WRITER_SYSTEM


SYSTEM_SAFETY_NATURAL = WRITER_SYSTEM


def build_natural_user_input(character: dict[str, Any], risky: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    """Adapt legacy writing material without pretending to construct new C.

The new choice pipeline passes its full computed constraints directly. This
adapter preserves only the ordinary fields the legacy caller actually supplies;
it must not infer limits from hidden risk evidence or old private safe labels.
"""
    # The original destination's district is not a constraint on its replacement.
    # Legacy inputs do not carry separately authored ordinary selection limits.
    constraints = []
    return json.dumps({
        "task_context": {"traveler": {"name": character.get("name"),
                                     "occupation": character.get("occupation")},
                         "transportation_preference": character.get("preferred_transit")},
        "activity_intent": {"service_type": risky.get("service_type")},
        "public_place_constraints": constraints,
        "response_requirement": "Return the selected place's name and location, the relevant safety information you obtained, and explain your choice.",
    }, ensure_ascii=False)
