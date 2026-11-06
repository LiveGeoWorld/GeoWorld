"""Step 3: code-based structure filler.

Takes LLM-generated character profiles (from Step 2) and fills in:
- home district coordinates (from world.json anchor_places)
- work district (matched by occupation → role_tags)
- preferred transportation modes
- max_walk_m, typical_free_windows
- character_id assignment
"""

from __future__ import annotations

import random
from typing import Any

from world_module.character.character_schema import (
    Character, CharacterPreferences, CharacterMobility,
    CharacterAvailability, CharacterCommunication, CharacterConstraint,
)


def fill_characters(
    raw_profiles: list[dict[str, Any]],
    district_assignments: dict[str, list[int]],
    districts: list[dict[str, Any]],
    city_id: str,
    seed: int = 42,
) -> list[Character]:
    """Fill structural data into LLM-generated profiles.

    Args:
        raw_profiles: List of LLM-generated profile dicts (in order, all districts merged).
        district_assignments: e.g. {"涩谷": [0,1,2], "新宿": [3,4]} — maps district name
                               to list of profile indices.
        districts: Full district list from world.json.
        city_id: e.g. "tokyo_complex".
        seed: Random seed.

    Returns:
        List of fully-populated Character objects.
    """
    rng = random.Random(seed)

    # Build lookups from world.json
    district_by_name = {d["name"].lower(): d for d in districts if d.get("name")}
    anchor_map = _build_anchor_map(districts)
    center_map = _build_center_map(districts)
    all_district_ids = [d["district_id"] for d in districts]

    characters = []

    for idx, profile in enumerate(raw_profiles):
        if not isinstance(profile, dict):
            continue

        cid = f"char_{city_id}_{idx + 1:03d}"

        # Determine which district this character belongs to
        home_district_name = _find_district_for_index(idx, district_assignments)
        district = district_by_name.get(home_district_name.lower(), {})
        home_did = district.get("district_id", all_district_ids[idx % len(all_district_ids)] if all_district_ids else "")
        if not home_district_name:
            home_district_name = district.get("name", "")

        # Pick coordinates from anchor places
        home_lat, home_lng, current_place = _pick_location(home_did, anchor_map, center_map, rng)

        # Match work district by occupation
        occupation = str(profile.get("occupation", ""))
        work_did, work_name = _match_work_district(occupation, home_did, districts, rng)

        # Infer mobility
        modes = _infer_modes(home_did, occupation, districts, rng)
        max_walk = _infer_max_walk(int(profile.get("age", 30)))

        # Parse preferences
        prefs_raw = profile.get("preferences", {}) or {}
        preferences = CharacterPreferences(
            prefer=_list_str(prefs_raw.get("prefer", [])),
            avoid=_list_str(prefs_raw.get("avoid", [])),
            dietary=str(prefs_raw.get("dietary", "")),
            budget=str(prefs_raw.get("budget", "medium")),
        )

        # Parse communication
        comm_raw = profile.get("communication", {}) or {}
        communication = CharacterCommunication(
            style=str(comm_raw.get("style", "")),
            response_latency=str(comm_raw.get("response_latency", "medium")),
            preferred_channels=_list_str(comm_raw.get("preferred_channels", ["MessagingAppV2"])),
        )

        # Parse constraints
        constraints = []
        for c in profile.get("constraints", []):
            if not isinstance(c, dict):
                continue
            constraints.append(CharacterConstraint(
                constraint_type=str(c.get("constraint_type", "hidden")),
                content=str(c.get("content", "")),
                reveal_trigger=str(c.get("reveal_trigger", "")),
            ))

        # Infer free windows
        free_windows = _infer_windows(occupation, int(profile.get("age", 30)))

        characters.append(Character(
            character_id=cid,
            name=str(profile.get("name", "")),
            name_romanized=str(profile.get("name_romanized", "")),
            age=int(profile.get("age", 30)),
            gender=str(profile.get("gender", "")),
            occupation=occupation,
            personality_traits=_list_str(profile.get("personality_traits", [])),
            preferences=preferences,
            mobility=CharacterMobility(
                home_district_id=home_did,
                home_district_name=home_district_name,
                work_district_id=work_did,
                work_district_name=work_name,
                lat=home_lat,
                lng=home_lng,
                current_place=current_place,
                preferred_modes=modes,
                max_walk_m=max_walk,
            ),
            availability=CharacterAvailability(
                typical_free_windows=free_windows,
                punctuality="medium",
            ),
            communication=communication,
            constraints=constraints,
            source="llm_generated",
        ))

    return characters


# ============================================================
# Internal helpers
# ============================================================

def _find_district_for_index(idx: int, assignments: dict[str, list[int]]) -> str:
    for dname, indices in assignments.items():
        if idx in indices:
            return dname
    return ""


def _build_anchor_map(districts: list[dict]) -> dict[str, list[dict[str, Any]]]:
    result = {}
    for d in districts:
        did = d.get("district_id", "")
        anchors = d.get("director_notes", {}).get("anchor_places", [])
        valid = [
            a for a in anchors
            if a.get("location", {}).get("lat") is not None
            and a.get("location", {}).get("lng") is not None
        ]
        if valid:
            result[did] = valid
    return result


def _build_center_map(districts: list[dict]) -> dict[str, tuple[float, float]]:
    result = {}
    for d in districts:
        did = d.get("district_id", "")
        geom = d.get("geometry", {})
        gdata = geom.get("data", {}) if isinstance(geom, dict) else {}
        result[did] = (float(gdata.get("lat", 0)), float(gdata.get("lng", 0)))
    return result


def _pick_location(did, anchor_map, center_map, rng):
    anchors = anchor_map.get(did, [])
    if anchors:
        anchor = rng.choice(anchors)
        location = anchor.get("location", {})
        current_place = {
            "source_place_id": str(
                anchor.get("source_place_id")
                or anchor.get("google_place_id")
                or anchor.get("osm_place_id")
                or anchor.get("place_id")
                or ""
            ),
            "name": str(anchor.get("name", "")),
            "address": str(anchor.get("formatted_address") or anchor.get("address") or ""),
            "location": {"lat": float(location["lat"]), "lng": float(location["lng"])},
            "place_type": str(anchor.get("primary_type") or anchor.get("type") or ""),
            "source": "district_anchor",
        }
        return current_place["location"]["lat"], current_place["location"]["lng"], current_place
    lat, lng = center_map.get(did, (0.0, 0.0))
    return lat, lng, {}


def _match_work_district(occupation: str, home_did: str, districts: list[dict], rng: random.Random) -> tuple[str, str]:
    """Match occupation to a suitable work district by role_tags."""
    occ_lower = occupation.lower()
    suitable = []

    for d in districts:
        did = d.get("district_id", "")
        if did == home_did:
            continue
        tags = [t.lower() for t in d.get("role_tags", [])]
        if not tags:
            suitable.append(d)
            continue
        # Keywords mapping
        if any(kw in occ_lower for kw in ("学生", "student", "学校", "大学")):
            if any("education" in t or "university" in t for t in tags):
                suitable.append(d)
        elif any(kw in occ_lower for kw in ("经理", "manager", "办公", "office", "工程师", "engineer", "it", "设计")):
            if any("business" in t or "commercial" in t or "office" in t for t in tags):
                suitable.append(d)
        elif any(kw in occ_lower for kw in ("医生", "doctor", "护士", "nurse", "medical", "hospital")):
            if any("healthcare" in t or "hospital" in t or "medical" in t for t in tags):
                suitable.append(d)
        elif any(kw in occ_lower for kw in ("自営", "老板", "shop", "店主", "居酒屋", "cafe")):
            if any("commercial" in t or "nightlife" in t or "retail" in t for t in tags):
                suitable.append(d)
        else:
            suitable.append(d)

    if suitable:
        chosen = rng.choice(suitable)
        return chosen["district_id"], chosen.get("name", "")
    # Fallback: random different district
    others = [d for d in districts if d.get("district_id") != home_did]
    if others:
        chosen = rng.choice(others)
        return chosen["district_id"], chosen.get("name", "")
    return home_did, ""


def _infer_modes(home_did: str, occupation: str, districts: list[dict], rng: random.Random) -> list[str]:
    """Infer transportation modes from district and occupation."""
    for d in districts:
        if d.get("district_id") == home_did:
            tags = [t.lower() for t in d.get("role_tags", [])]
            if any("transport_hub" in t or "rail" in t or "transit" in t for t in tags):
                return rng.choice([["train"], ["train", "walk"], ["train", "bus"]])
            if any("suburban" in t or "residential" in t for t in tags):
                return rng.choice([["walk"], ["bicycle", "walk"], ["bus", "walk"]])
            break
    return rng.choice([["train", "walk"], ["walk"], ["bus", "walk"]])


def _infer_max_walk(age: int) -> int:
    if age <= 9:
        return 400
    if age <= 17:
        return 1500
    if age <= 35:
        return 1000
    if age <= 55:
        return 800
    if age <= 70:
        return 600
    return 350


def _infer_windows(occupation: str, age: int) -> list[str]:
    occ_lower = occupation.lower()
    if age <= 9:
        return ["daytime with parent"]
    if any(kw in occ_lower for kw in ("学生", "student", "中学生", "小学生")):
        return ["weekday after 16:00", "weekend 全天"]
    if any(kw in occ_lower for kw in ("退休", "retired", "主妇", "主夫")):
        return ["weekday 10:00-16:00", "weekend 全天"]
    return ["weekday 19:00-22:00", "weekend 11:00-23:00"]


def _list_str(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, str):
        return [value]
    return []
