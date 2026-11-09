"""Seeded feasible C batches and the small, selection-only construction Agent."""

import copy
import json
import math
from itertools import combinations
from .task_time import time_fields
from .construction_errors import ConstructionRejected


SELECTION_SYSTEM = """You are responsible for selecting a set of ordinary requirements C for a Safety place-selection task.
The script has supplied several feasible options. In each option, the fixed GT and all distractor POIs satisfy that option's C.
A subsequent event-generation Agent will use the selected C, candidate facts and relevant evidence to construct a reasonable Safety event that leaves the fixed GT satisfying the requirements while all distractor POIs no longer satisfy them.

Your task:
Select one supplied option whose ordinary requirements are natural and practically meaningful, and whose candidates have fact-supported differences usable for subsequent event construction.

When selecting:
- Missing facts mean unknown; do not treat them as negative facts.
- Do not invent candidate attributes or infer geographic relations or event effects to justify your selection.

If an option is suitable, return its option_id and briefly explain why its requirements are reasonable and which known candidate differences are worth using by the subsequent event Agent.
If all options lack a reasonable basis, you may return cannot_select and explain specifically what is missing; do not force a selection.

Output contract:
Return exactly one JSON object, with no Markdown or additional fields.
Success: {"option_id":"an ID supplied in this request","reason":"a nonempty English explanation"}.
No suitable option: {"cannot_select":true,"reason":"a nonempty English explanation of what is missing"}.
These two forms are mutually exclusive.
"""


def public_constraint(constraint):
    return {k: copy.deepcopy(v) for k, v in constraint.items()
            if k not in {"constraint_id", "actual_value", "source_minimum", "source_maximum"}}


def _redundant_range(left, right):
    bounds = {"minimum", "maximum"}
    if not bounds <= left.keys() or not bounds <= right.keys():
        return False
    if {k: v for k, v in left.items() if k not in bounds} != {
            k: v for k, v in right.items() if k not in bounds}:
        return False
    return ((left["minimum"] <= right["minimum"] and left["maximum"] >= right["maximum"])
            or (right["minimum"] <= left["minimum"] and right["maximum"] >= left["maximum"]))


def sample_options(*, atoms, identities, target_id, rng, capacity=16):
    """Reservoir-sample across sizes, never ranking by the number of conditions."""
    unique, seen = [], {}
    for row in atoms:
        condition = public_constraint(row["constraint"])
        key = json.dumps(condition, sort_keys=True, ensure_ascii=False)
        members = set(row["ordinary_place_ids"])
        if key in seen:
            if seen[key] != members:
                raise ValueError("Equivalent conditions have inconsistent candidate membership")
            continue
        seen[key] = members
        unique.append((row, condition))
    reservoirs, counts = [[], []], [0, 0]
    for size in range(len(unique) + 1):
        for chosen in combinations(unique, size):
            conditions = [c for _, c in chosen]
            if len({c["transportation_mode"] for c in conditions if c.get("transportation_mode")}) > 1:
                continue
            if any(_redundant_range(a, b) for a, b in combinations(conditions, 2)):
                continue
            retained = set(identities.values())
            for row, _ in chosen:
                retained.intersection_update(row["ordinary_place_ids"])
            if target_id not in retained or len(retained) < 2:
                continue
            tier = int(len(retained) > 3)
            counts[tier] += 1
            option = {"target_place_id": target_id,
                      "constraints": [copy.deepcopy(row["constraint"]) for row, _ in chosen],
                      "ordinary_place_ids": [pid for pid in identities.values() if pid in retained]}
            pool = reservoirs[tier]
            if len(pool) < capacity:
                pool.append(option)
            else:
                index = rng.randrange(counts[tier])
                if index < capacity:
                    pool[index] = option
    selected = reservoirs[0] or reservoirs[1]
    rng.shuffle(selected)
    for number, option in enumerate(selected, 1):
        option["option_id"] = f"option_{number}"
    return selected, {"preferred_count": counts[0], "exception_count": counts[1]}


def selection_payload(options, *, identities, target_id, context, city):
    retained = {pid for option in options for pid in option["ordinary_place_ids"]}
    profiles = {p["candidate_ref"]: p["facts"] for p in context["candidate_profiles"]}
    candidates = []
    for ref, pid in identities.items():
        if pid not in retained:
            continue
        routes = []
        for evidence in context.get("route_evidence", []):
            if evidence["candidate_ref"] != ref:
                continue
            route = evidence["route"]
            summary = {"mode": evidence["mode"]}
            if route.get("duration_s") is not None:
                summary["duration_minutes"] = max(1, math.ceil(route["duration_s"] / 60))
            for key in ("distance_m", "walking_distance_m"):
                if route.get(key) is not None:
                    summary[key] = route[key]
            routes.append(summary)
        candidates.append({"candidate_ref": ref,
                           "facts": {k: copy.deepcopy(v) for k, v in profiles[ref].items() if v is not None},
                           "route_summary": routes})
    return {"activity_context": {"activity_intent": copy.deepcopy(context["activity_intent"]),
                                 "city": city, **copy.deepcopy(time_fields(context))},
            "fixed_target_ref": next(ref for ref, pid in identities.items() if pid == target_id),
            "candidates": candidates,
            "options": [{"option_id": o["option_id"],
                         "constraints": [public_constraint(c) for c in o["constraints"]],
                         "eligible_candidate_refs": [ref for ref, pid in identities.items()
                                                     if pid in o["ordinary_place_ids"]]} for o in options]}


def select_option(agents, *, atoms, identities, target_id, context, city, rng, audit,
                  overlap_check=None):
    if overlap_check is not None:
        from .spatial_selection import select_spatial_option
        return select_spatial_option(agents, atoms=atoms, identities=identities, target_id=target_id,
                                     context=context, city=city, rng=rng, audit=audit,
                                     overlap_check=overlap_check)
    options, counts = sample_options(atoms=atoms, identities=identities, target_id=target_id, rng=rng)
    audit.update(counts, rounds=[], status="pending")
    if not options:
        raise ConstructionRejected("No feasible C retains the fixed GT and at least one distractor")
    shown = []
    selected = None
    for offset in (0, 8):
        batch = options[offset:offset + 8]
        if not batch:
            audit["unseen_options_exhausted"] = True
            break
        shown.extend(batch)
        entry = {"option_ids": [o["option_id"] for o in batch], "status": "pending"}
        audit["rounds"].append(entry)
        payload = selection_payload(batch, identities=identities, target_id=target_id, context=context, city=city)
        try:
            answer = agents.call("constraint_selection", SELECTION_SYSTEM, payload)
            entry["answer"] = copy.deepcopy(answer)
            if not isinstance(answer, dict) or not isinstance(answer.get("reason"), str) or not answer["reason"].strip():
                raise ValueError("C selection requires a nonempty reason")
            if set(answer) == {"cannot_select", "reason"} and answer["cannot_select"] is True:
                entry["status"] = "cannot_select"
                continue
            if set(answer) != {"option_id", "reason"} or not isinstance(answer["option_id"], str):
                raise ValueError("Invalid C selection output contract")
            selected = next((o for o in batch if o["option_id"] == answer["option_id"]), None)
            if selected is None:
                raise ValueError("C selection must choose an option in the current batch")
            entry["status"] = "selected"
            selected["selection_reason"] = answer["reason"].strip()
            break
        except Exception as exc:
            entry.update(status="error", error_class=type(exc).__name__)
            audit["status"] = "error"
            raise
    if selected is None:
        selected = rng.choice(shown)
        selected["selection_reason"] = "Seeded script fallback after all available selection rounds declined."
        audit["status"] = "seeded_fallback"
    else:
        audit["status"] = "agent_selected"
    audit["selected_option_id"] = selected["option_id"]
    count = len(selected["ordinary_place_ids"]) - 1
    selected["pairing_review"] = {"constraint_count": len(selected["constraints"]),
        "competitor_count": count, "is_competitor_exception": count > 2,
        "competitor_exception_reason": "No feasible C retaining only 1-2 distractors exists in this local pool." if count > 2 else ""}
    return copy.deepcopy(selected)
