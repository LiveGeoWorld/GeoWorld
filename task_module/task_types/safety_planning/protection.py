"""Private state-and-scope protection, without prescribing event stories."""

from datetime import datetime

from task_module.task_types.safety_planning.spacetime import TimeWindow
from .task_time import applies


class ProtectionConflict(ValueError):
    """A source-checked conflicting relation, not an ordinary pairing failure."""

    def __init__(self, record_refs, details=None):
        self.record_refs = sorted(set(record_refs))
        self.details = list(details or [])
        super().__init__("Existing Safety protection conflicts: " + ", ".join(self.record_refs))


PROTECTION_SYSTEM = """
Return protected_states inside event, covering the fixed GT and every retained core competitor.
Each entry has candidate_ref, scope_refs (supplied geographic anchor references), active_window,
state, decision_requirement, source_refs and reason. State and decision_requirement are specific
natural-language descriptions, not a fixed event/state vocabulary. Include the relevant normal
state supporting the GT as well as the incident state excluding competitors. Cover the activity
window. Protect only the scope and state actually needed by the selected C and event. Do not
claim externally verified normal conditions merely because no incident was recorded. A point
does not define a building, utility network or area boundary; use only supported scope precision.
These entries are private and must not appear in the public notice.

prior_spacetime_records are already filtered private events and colored decision dependencies.
Keep the event inside event_generation_window.
Do not rewrite their states or targets. For every time-applicable record/direction, return one
avoidance_assessments entry with record_ref, direction, relation, source_refs and reason.
Use new_event_to_prior_record when the new event overlaps a record's active_window; also use
prior_event_to_current_dependencies for each old event overlapping the current activity_window.
Cite current and prior sources. relation=disjoint means source-supported spatial separation.
For records carrying protection_contract=safety_state_protection.v1, relation=compatible may describe overlapping scopes when
the protected state AND original decision remain unchanged; explicitly return preserves_state=true
and preserves_decision=true and explain both. In the reverse direction, these refer to the new
task's protected states and decision; for old event records, preserve the existing incident too.
Cite current sources belonging to the selected candidates or their selected protected scopes.
Do not use compatibility for cumulative-delay/slack
reasoning. Legacy uncolored records still require disjointness. Unknown is never harmless.
The public notice contains none of these assessments, references, or protection information.
If a grounded compatible construction is impossible, return cannot_construct with the reason.
If C and a complete event would work on their own but existing records block that pair,
return that full pair with source-grounded conflicting avoidance assessments (relation=intersects,
or compatible with preserves_state/preserves_decision=false). Do not pretend disjointness.
The script verifies the standalone choice before counting a protection conflict. A missing fact,
unknown relation, malformed response or ordinary pairing failure is not cluster saturation.
"""


def validate_protections(event: dict, context: dict) -> None:
    if context.get("protection_contract") in {"safety_occupancy.v5", "safety_occupancy.v6"}:
        from .gt_protection import compile_protection
        resolved = compile_protection(event, context)
        if resolved["compiled_gt_protection"] != context.get("compiled_gt_protection"):
            raise ValueError("Missing compiled GT protection")
        return
    if "events" in event:
        from .event_bundle import events
        combined = {"protected_states": [r for item in events(event) for r in item.get("protected_states", [])]}
        return validate_protections(combined, context)
    if context.get("protection_contract") not in {"safety_state_protection.v1", "safety_occupancy.v1", "safety_occupancy.v2", "safety_occupancy.v3", "safety_occupancy.v4"}:
        return
    profiles = {row["candidate_ref"] for row in context["candidate_profiles"]}
    anchors = {row["scope_ref"]: row for row in context["geographic_anchors"]}
    rows = event.get("protected_states")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Joint event requires explicit protected states")
    activity = None if "task_time" in context else TimeWindow(**context["activity_window"])
    parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("candidate_ref") not in profiles:
            raise ValueError("Protection must bind a retained core candidate")
        seen.add(row["candidate_ref"])
        scopes = row.get("scope_refs")
        if (not isinstance(scopes, list) or not scopes
                or any(not isinstance(ref, str) or ref not in anchors for ref in scopes)):
            raise ValueError("Protection requires supplied scope references")
        refs = row.get("source_refs")
        scope_sources = {ref for scope in scopes for ref in anchors[scope].get("source_refs", [])}
        if (not isinstance(refs, list) or not refs
                or any(not isinstance(ref, str) or ref not in scope_sources for ref in refs)
                or any(not set(refs).intersection(anchors[scope].get("source_refs", [])) for scope in scopes)):
            raise ValueError("Protection must cite every selected scope's frozen sources")
        if any(not isinstance(row.get(key), str) or not row[key].strip()
               for key in ("state", "decision_requirement", "reason")):
            raise ValueError("Protection requires explicit state and decision reasoning")
        window = row.get("active_window")
        if not isinstance(window, dict) or set(window) != {"start", "end"}:
            raise ValueError("Protection requires an effective time interval")
        if any(not isinstance(value, str) for value in window.values()):
            raise ValueError("Protection times must be ISO strings")
        protected = TimeWindow(**window)
        if "task_time" in context:
            if not applies(window, context["task_time"]):
                raise ValueError("Protection must contain the task time")
        elif not (parse(protected.start) <= parse(activity.start)
                and parse(protected.end) >= parse(activity.end)):
            raise ValueError("Protection must cover the task activity window")
    if seen != profiles:
        raise ValueError("Protection must cover GT and every retained core competitor")
