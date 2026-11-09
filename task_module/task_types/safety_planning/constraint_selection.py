"""Let the existing construction Agent choose ordinary C, never the Writer."""

import copy


CONSTRAINT_SYSTEM = """You are the existing Safety construction Agent choosing ordinary place requirements C before authoring a safety event.
The private target is fixed. The activity is supported by every supplied candidate. Each available constraint is computed from frozen facts and lists the candidates it retains. Select a coherent set of these constraints by their exact IDs. Their intersection must retain the fixed target AND at least one other candidate. Ordinary C must NOT identify the target by itself: a later realistic safety event must be necessary to distinguish it.
Consider the supplied source-backed candidate differences and route requirements when deciding which shared constraints can support such an event. Do not prefer constraints merely because they appear first. Additional ordinary constraints are optional. If you select transportation constraints, use one consistent mode. Do not invent a condition, change any boundary, infer missing facilities, or change the target. Previous failed attempts are diagnostic context, not source facts.
Return JSON: {"constraint_ids":["exact supplied ID"],"reason":"why these shared requirements and the supplied differences permit a realistic safety distinction"}.
This is private construction reasoning, not the public event or task. The event author will establish the actual event and exposure; scripts will verify C plus that event uniquely retains the fixed target.
"""


def select_constraints(agents, *, atoms, identities, target_id, activity_intent, candidates, failures):
    by_id = {row["constraint"]["constraint_id"]: row for row in atoms}
    if len(by_id) != len(atoms):
        raise ValueError("Ordinary constraint IDs must be unique")
    target_ref = next(ref for ref, pid in identities.items() if pid == target_id)
    payload = {"target_ref": target_ref, "activity_intent": activity_intent,
               "candidates": candidates, "previous_attempts": copy.deepcopy(failures),
               "available_constraints": [{"constraint": row["constraint"],
                   "retained_candidates": [ref for ref, pid in identities.items() if pid in row["ordinary_place_ids"]]}
                   for row in atoms]}
    answer = agents.call("constraint_selection", CONSTRAINT_SYSTEM, payload)
    return resolve_constraints(answer, atoms=atoms, identities=identities, target_id=target_id)


def resolve_constraints(answer, *, atoms, identities, target_id):
    """Resolve model-selected IDs against frozen intersections."""
    by_id = {row["constraint"]["constraint_id"]: row for row in atoms}
    if len(by_id) != len(atoms):
        raise ValueError("Ordinary constraint IDs must be unique")
    selected = answer.get("constraint_ids")
    if (not isinstance(selected, list) or any(not isinstance(cid, str) or cid not in by_id for cid in selected)
            or len(selected) != len(set(selected))):
        raise ValueError("Constraint selection must reference distinct computed conditions")
    if not isinstance(answer.get("reason"), str) or not answer["reason"].strip():
        raise ValueError("Constraint selection requires reasoning")
    conditions = [copy.deepcopy(by_id[cid]["constraint"]) for cid in selected]
    if len({c["transportation_mode"] for c in conditions if c.get("transportation_mode")}) > 1:
        raise ValueError("Selected C mixes transportation modes")
    retained = set(identities.values())
    for cid in selected:
        retained.intersection_update(by_id[cid]["ordinary_place_ids"])
    if target_id not in retained or len(retained) < 2:
        raise ValueError("Ordinary C must retain the fixed GT and at least one competitor")
    return {"target_place_id": target_id, "constraints": conditions,
            "ordinary_place_ids": [pid for pid in identities.values() if pid in retained],
            "selection_reason": answer["reason"]}
