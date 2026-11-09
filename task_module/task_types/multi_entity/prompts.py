"""Response parsing for the Multi-Entity natural-language writer."""

from __future__ import annotations

import json
import re


def parse_llm_response(text: str) -> dict | None:
    """Parse one JSON object, with optional Markdown code fences."""
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        text = match.group(1).strip()

    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    end = start
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    try:
        parsed = json.loads(text[start:end])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
