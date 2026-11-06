"""Read-only author review projection; never solve or repair a Safety task."""

from pathlib import Path

from shared.io import read_json
from app.apps.safety_event_view import load_safety_event_view


def safety_review_data(root: Path, sandbox: dict) -> dict:
    decision = sandbox.get("safety_choice")
    review = {"version": "choice" if isinstance(decision, dict) else "legacy",
              "warnings": [], "records": {}, "event_view": None}
    for name in ("static_app_state.json", "generation_log/details.json",
                 "generation_log/llm_in.json", "generation_log/llm_out.json",
                 "generation_log/writer_messages.json"):
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()):
            review["warnings"].append(f"Blocked non-local review record: {name}")
            continue
        if path.is_file():
            try:
                review["records"][name] = read_json(path)
            except (ValueError, OSError) as exc:
                review["warnings"].append(f"Cannot read {name}: {type(exc).__name__}")
    try:
        review["event_view"] = load_safety_event_view(root)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        review["warnings"].append(f"Safety event view unavailable: {type(exc).__name__}")
    return review


def safety_edit_error(sandbox: dict, fields: dict) -> str | None:
    if isinstance(sandbox.get("safety_choice"), dict) and set(fields) - {"user_prompt"}:
        return "Safety choice evidence is read-only; only user_prompt may be edited."
    return None
