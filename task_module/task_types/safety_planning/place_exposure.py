"""Validate model-authored place exposure against supplied location evidence."""


def resolve_place_exposure(event: dict, context: dict) -> set[str]:
    places = {row["candidate_ref"]: row for row in context["place_evidence"]}
    candidates = {row["candidate_ref"] for row in context["candidate_profiles"]}
    if places.keys() != candidates:
        raise ValueError("Place evidence must cover every ordinary candidate")
    rows = event.get("place_exposure")
    if not isinstance(rows, list):
        raise ValueError("Event Agent must return private place_exposure")
    seen, members = set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Place exposure must be an object")
        ref = row.get("candidate_ref")
        if not isinstance(ref, str) or ref not in places or ref in seen or row.get("scope_ref") != event["scope_ref"]:
            raise ValueError("Unknown, duplicate or mismatched place exposure")
        seen.add(ref)
        if row.get("status") not in {"intersects", "disjoint"}:
            raise ValueError("Unknown place exposure cannot prove the fixed GT")
        citations = row.get("source_refs")
        if (not isinstance(citations, list) or not citations
                or any(not isinstance(item, str) or item not in places[ref]["source_refs"] for item in citations)):
            raise ValueError("Place exposure must cite its own frozen location evidence")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ValueError("Place exposure requires a source-grounded explanation")
        if row["status"] == "intersects":
            members.add(ref)
    if seen != candidates:
        raise ValueError("Place exposure must cover every ordinary candidate")
    return members
