"""Bounded C selection with script-owned occupancy checks and audited feedback."""

import copy

from .option_selection import SELECTION_SYSTEM, sample_options, selection_payload
from .construction_errors import ConstructionRejected

MAX_SELECTIONS_PER_BATCH = 3
MAX_C_BATCHES = 3
BATCH_SIZE = 8


class SpatialOptionsExhausted(ValueError):
    """All sampled options are proven occupied; not a model refusal."""

    def __init__(self, conflicts):
        self.conflicts = copy.deepcopy(conflicts)
        super().__init__("Sampled C options exhausted by coloring overlap")


def select_spatial_option(agents, *, atoms, identities, target_id, context, city, rng, audit,
                          overlap_check):
    """overlap_check returns only proven conflicts; unknown/error must raise.

    The callback receives an option and returns candidate/source conflict records,
    without descriptions of old events. It cannot remove C-retained candidates.
    """
    options, counts = sample_options(atoms=atoms, identities=identities, target_id=target_id,
                                    rng=rng, capacity=BATCH_SIZE * MAX_C_BATCHES)
    audit.update(counts, rounds=[], spatial_checks=[], status="pending",
                 limits={"selections_per_batch": MAX_SELECTIONS_PER_BATCH, "c_batches": MAX_C_BATCHES})
    if not options:
        audit["status"] = "no_feasible_options"
        raise ConstructionRejected("No feasible C retains the fixed GT and at least one distractor")
    checked, blocked, shown = {}, {}, {}
    refusals = 0

    def checkpoint():
        callback = getattr(agents, "_checkpoint", None)
        if callback:
            callback()

    def inspect(option):
        oid = option["option_id"]
        if oid not in checked:
            conflicts = overlap_check(copy.deepcopy(option))
            if not isinstance(conflicts, list):
                raise ValueError("Occupancy checker must return a list of proven conflicts")
            checked[oid] = copy.deepcopy(conflicts)
            audit["spatial_checks"].append({"option_id": oid, "conflicts": copy.deepcopy(conflicts)})
            if conflicts:
                blocked[oid] = conflicts
            checkpoint()
        return checked[oid]

    def finish(option, reason, status):
        result = copy.deepcopy(option)
        result["selection_reason"] = reason
        count = len(result["ordinary_place_ids"]) - 1
        result["pairing_review"] = {"constraint_count": len(result["constraints"]),
            "competitor_count": count, "is_competitor_exception": count > 2,
            "competitor_exception_reason": "No feasible C retaining only 1-2 distractors exists in this local pool." if count > 2 else ""}
        audit.update(status=status, selected_option_id=result["option_id"])
        checkpoint()
        return result

    def fallback():
        safe = [option for option in shown.values() if not inspect(option)]
        if safe:
            return finish(rng.choice(safe), "Seeded fallback among shown options passing the occupancy gate.", "seeded_fallback")
        return None

    try:
        for batch_index in range(MAX_C_BATCHES):
            batch = options[batch_index * BATCH_SIZE:(batch_index + 1) * BATCH_SIZE]
            if not batch:
                audit["unseen_options_exhausted"] = True
                break
            for selection_index in range(MAX_SELECTIONS_PER_BATCH):
                available = [o for o in batch if o["option_id"] not in blocked]
                if not available:
                    break
                if refusals >= 2:
                    # The ordinary-refusal contract permits no additional model calls.
                    result = fallback()
                    if result:
                        return result
                    audit["status"] = "refusal_fallback_blocked"
                    raise ConstructionRejected("Ordinary refusal fallback has no shown non-overlapping option")
                payload = selection_payload(available, identities=identities, target_id=target_id,
                                            context=context, city=city)
                if blocked:
                    payload["overlap_feedback"] = {"reason": "These options overlap prefiltered coloring; select only a currently supplied option.",
                        "excluded_options": [{"option_id": oid, "conflicts": [
                            {key: copy.deepcopy(row[key]) for key in ("candidate_ref", "source_ref", "dependency_kind") if key in row}
                            for row in rows]} for oid, rows in blocked.items()]}
                shown.update({o["option_id"]: o for o in available})
                entry = {"batch": batch_index + 1, "selection": selection_index + 1,
                         "option_ids": [o["option_id"] for o in available], "status": "requesting"}
                audit["rounds"].append(entry)
                checkpoint()
                answer = agents.call("constraint_selection", SELECTION_SYSTEM, payload)
                entry["answer"] = copy.deepcopy(answer)
                if not isinstance(answer, dict) or not isinstance(answer.get("reason"), str) or not answer["reason"].strip():
                    raise ValueError("C selection requires a nonempty reason")
                if set(answer) == {"cannot_select", "reason"} and answer["cannot_select"] is True:
                    refusals += 1
                    entry["status"] = "cannot_select"
                    checkpoint()
                    break
                if set(answer) != {"option_id", "reason"} or not isinstance(answer["option_id"], str):
                    raise ValueError("Invalid C selection output contract")
                selected = next((o for o in available if o["option_id"] == answer["option_id"]), None)
                if selected is None:
                    raise ValueError("C selection must choose an option in the current batch")
                if not inspect(selected):
                    entry["status"] = "selected"
                    return finish(selected, answer["reason"].strip(), "agent_selected")
                entry["status"] = "overlap_rejected"
                # A script can check the whole batch; do not make the model hit
                # the same occupied dependency through each equivalent pairing.
                for option in batch:
                    inspect(option)
                checkpoint()
        if refusals:
            result = fallback()
            if result:
                return result
            audit["status"] = "refusal_fallback_blocked"
            raise ConstructionRejected("Ordinary refusal fallback has no shown non-overlapping option")
        audit["status"] = "spatial_options_exhausted"
        raise SpatialOptionsExhausted(blocked)
    except Exception as exc:
        if audit["status"] == "pending":
            audit["status"] = "error"
        audit["error_class"] = type(exc).__name__
        raise
    finally:
        checkpoint()
