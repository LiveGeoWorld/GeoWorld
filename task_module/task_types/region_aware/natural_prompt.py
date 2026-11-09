"""Public wording for region-aware tasks without cultural-rule leakage."""

from __future__ import annotations

from typing import Any


SYSTEM_REGION_NATURAL = """Write a concise natural Chinese request for a real-world planning task.

The request must ask for a concrete executable itinerary: select real places, give an arrival time and a practical activity or travel sequence. It may say the visitor wants the plan to fit local context, but it must NOT ask the solver to list, explain, recite, or teach etiquette, customs, taboos, or a rule. Do not ask for \"proper behaviour\", \"what customs apply\", or examples of gestures and table manners.

Name the selected traveler from `traveler.name` in the first sentence. Write
about that named person in the third person; do not use first-person `I`,
`me`, `my`, `我`, `我们`, or an unnamed traveler. The named person anchors the
public task context.

When a destination requirement is provided, preserve that public place category exactly. Do not broaden it to a different kind of venue.

Do not invent places, facts, cultural rules, or answers. Do not reveal Wikivoyage, evidence cards, rule text, constraint IDs, hidden rules, candidate POIs, or private evaluation criteria. Return JSON only: {\"user_prompt\": \"...\"}."""


def build_natural_user_input(character: dict[str, Any], city: str, scenario: str,
                             public_activity: str = "local_visit",
                             destination_requirement: str = "") -> str:
    return str({
        "traveler": {"name": character.get("name"), "occupation": character.get("occupation"),
                     "home": character.get("home_place_name")},
        "city": city, "scenario": scenario or "general visit", "public_activity": public_activity,
        "destination_requirement": destination_requirement,
        "instruction": "Ask for place, time and executable itinerary decisions. Cultural reasoning remains implicit; do not ask the user to state customs or etiquette.",
    })
