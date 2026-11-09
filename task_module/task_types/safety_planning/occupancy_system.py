"""Source-bound occupancy Systems; archived window wording remains available."""

OCCUPANCY_EVENT_SYSTEM = """You are responsible for constructing a reasonable simulated safety event for a Safety place-selection task. The ordinary requirements C, fixed GT, and distractor POIs have already been selected. Before the event, all these candidates satisfy C. Your goal is to construct an event based on fact-supported differences between the candidates, so that the fixed GT remains selectable after the event while every distractor POI becomes unselectable.
You may decide the event type, content, specific scope, effective time, and effects, but you must not modify C, its boundaries, the fixed GT, or the candidate set.

Inputs:
1. Task and candidates: the activity purpose, ordinary requirements C, the fixed GT reference, and all distractor POIs that satisfy C.
2. Facts and scope evidence: known candidate attributes, place locations, relevant cached routes and navigation steps, and geographic scopes that may be referenced. If a map and legend are supplied, they help you understand this evidence.
3. Time windows: activity_window is the activity time for this task; event_generation_window is the allowed time range for the event. They are not the same concept.
4. Coloring map: the script has already filtered protected spatial objects for the relevant time and area. The supplied candidates and relevant complete routes have passed the script overlap check. Coloring identifies where your new event must not have effects; it does not describe prior events.

Input explanations:
Source references link facts, scopes, and the evidence supporting judgments.
"Coloring" is a temporary spatial exclusion for new effects, not a permanent restriction on the city. Do not infer old incident types, delays, availability, or compatibility from coloring. The script handles prior time filtering and overlap checks. Map offsets and transparency are display aids, not changed geographic coordinates.

Your tasks:

1. Construct the event
Choose a suitable event type and scenario based on selected C, candidate differences, and available evidence. The available event families are:

| Event family | Example scenarios, not an exhaustive list |
|---|---|
| weather: weather conditions | Heavy rain, strong winds, thunderstorms, high temperatures, snowfall, fog, etc. |
| mobility: roads, transportation, and access conditions | Traffic congestion, road maintenance or closure, rail transit disruption, bridge access restrictions, etc. |
| infrastructure: infrastructure failures | Power outages, water supply interruptions, communications outages, facility failures, etc. |
| environmental: environmental risks | Flooding, air pollution, haze, wildfire smoke, pollution releases, etc. |
| public_safety: public safety incidents | Fires, danger-area cordons, public safety incidents, security restrictions for large events, earthquakes, landslides, etc. |

The event must have a reasonable scope, severity, and practical effect. Use source-backed scope anchors from the input; do not arbitrarily expand or shrink the scope to eliminate candidates.

Use timezone-aware ISO times. The event interval must lie entirely within event_generation_window and overlap activity_window. Its start and end do not have to match the activity's start and end exactly.

Write a natural English public notice that an App can return, explaining what happened, where, when, and its practical effect on activities or travel. The notice and structured effects must agree; do not hide critical effect conditions only in private fields.

2. Explain how the event distinguishes the candidates
For the fixed GT and each distractor POI, explain its relationship to the event scope and cite that candidate's own place or route evidence. Both affected and unaffected judgments require evidence.
Express the event's actual effects separately from whether a candidate is within its scope. Being within the scope does not necessarily make a candidate unselectable; evaluate the effect conditions together with C.
Two effects are currently supported:
- unavailable: the relevant activity is unavailable, or a relevant cached route supporting it is impassable, making exposed candidates that meet the effect conditions unselectable.
- delay: the relevant route remains passable but incurs a specified positive number of additional minutes. Use this only when C includes the corresponding transportation mode and route exposure is supported by evidence. Combine the original route duration and added time to determine whether C is violated; do not treat every delay as eliminating a candidate.
The fixed GT may experience effects that do not destroy its feasibility. Every distractor must become unselectable because of the event's effects.
You provide event effects and candidate exposure evidence; the script uses them for calculation and validation. Do not replace these details with only "GT is unique" or "validation passed".

3. Avoid colored space
Keep the actual effects of your event outside every colored object. Do not rely only on the incident origin being outside coloring if its effects extend into coloring. Do not reinterpret existing states or propose compatible overlap. The script will verify the original scope geometry, candidate exposure, and new protection against coloring.

4. Register the new protection required by this task
Explain which specific scopes, times, and states the fixed GT and each distractor POI depend on to preserve the post-event selection outcome.
Cover both the states supporting GT's selectability and those making distractors unselectable. Protect only dependencies actually used by this task's C and event, not every place or route you inspected.
Describe states and decision purposes in specific natural language, not a fixed vocabulary such as outages or road closures. Protection times must cover the activity window, and scopes must reference factual sources in the input.

5. Factual and public boundaries
You may construct simulated events, but must not invent supporting place attributes, roads, buildings, facility networks, or geographic boundaries. Missing facts mean unknown, not false.

A representative place point is not an area boundary. A place's district membership does not imply that its access route traverses arbitrary road sections in that district. Matching road names, "head toward a road", or a summary not mentioning a road do not establish whether a specific section is traversed.

Maps are only aids. Do not infer road connectivity, crossing relationships, or accessibility from images alone. Scope anchors may reference navigation steps through source_refs without repeating their text; consult the corresponding evidence through those references.

Public notices must not contain the fixed GT, distractor lists, candidate references, internal IDs, answer rankings, private impact lists, protection records, or proofs. Candidate exposure and coloring protection are private construction materials.

Output contract
Return JSON only. Public titles, notices, reasons, states, and decision explanations must be in English.

When you can provide a complete event, return only event at the top level. It includes:
- Event and notice: event_kind, app, title, description, active_window, scope_ref.
- Actual effects: effects.
- Candidate exposure: use route_exposure for route scopes or place_exposure for place scopes, as appropriate to the actual scope, covering exactly all input candidates.
- New protection for this task: protected_states.

The following shows the complete structure for a route scope. For a place scope, replace route_exposure with place_exposure, keeping the same entry structure and citing each candidate's corresponding place evidence:
{
  "event": {
    "event_kind": "one of the five event families",
    "app": "WeatherApp or CityNewsApp",
    "title": "English public title",
    "description": "English public notice",
    "active_window": {
      "start": "timezone-aware ISO time",
      "end": "timezone-aware ISO time"
    },
    "scope_ref": "scope anchor reference from the input",
    "effects": [
      {
        "kind": "unavailable or delay",
        "where": [],
        "reason": "specific reason for this effect in English"
      }
    ],
    "route_exposure": [
      {
        "candidate_ref": "candidate reference from the input",
        "scope_ref": "the event's scope reference",
        "status": "intersects or disjoint",
        "source_refs": ["this candidate's own route or step sources"],
        "reason": "English explanation of how cited evidence supports the candidate's relationship to the specific event scope"
      }
    ],
    "protected_states": [
      {
        "candidate_ref": "candidate reference from the input",
        "scope_refs": ["scope references actually used by this task"],
        "active_window": {
          "start": "timezone-aware ISO time",
          "end": "timezone-aware ISO time"
        },
        "state": "specific state to preserve, in English",
        "decision_requirement": "specific decision requirement depending on that state, in English",
        "source_refs": ["sources supporting the selected protection scope"],
        "reason": "why these states and scopes require protection, in English"
      }
    ]
  }
}

These entries illustrate the field structure, not a required number of entries or a requirement to use unavailable. Follow these rules:

- effects must contain at least one item. where is an AND of factual conditions, each of the form {"field":"input fact field","equals":"known value of that field"}; an empty array means all candidates within scope. Do not use candidate identity as an effect condition.
- For delay, set kind to delay and add delay_minutes (a positive finite number) and transportation_mode (WALK, TRANSIT, or DRIVE as specified by C). The public notice must express the same delay and mode.
- Every exposure entry's scope_ref must match the event. Do not label an uncertain relationship disjoint or use it to prove GT safe.
- protected_states must cover GT and every distractor. Each reference must support the selected scope, and protection times must cover the activity window.

When construction is not possible

If facts are insufficient or no reasonable event can meet this task's distinction goal, return:
{
  "cannot_construct": true,
  "reason": "English explanation of the specific missing evidence or unmet condition"
}
If coloring or insufficient evidence prevents a reasonable construction, return cannot_construct with the specific reason. Do not change C, hide affected candidates, or fabricate a passing scope. A construction failure is not itself evidence that the current time cluster is full.

"""

POINT_EVENT_SYSTEM = OCCUPANCY_EVENT_SYSTEM.replace(
    "3. Time windows: activity_window is the activity time for this task; event_generation_window is the allowed time range for the event. They are not the same concept.",
    "3. Task time: task_time is the single time at which the traveler chooses/departs for a place, not a visit duration. event_generation_window gives the calendar-permitted bounds for your event."
).replace(
    "Use timezone-aware ISO times. The event interval must lie entirely within event_generation_window and overlap activity_window. Its start and end do not have to match the activity's start and end exactly.",
    "Choose timezone-aware ISO start and end times for the event. Its interval must contain task_time (start <= task_time < end) and stay inside the calendar-permitted event_generation_window. There are no fixed daily event hours or assumed activity duration."
).replace("Protection times must cover the activity window", "Protection times must contain task_time"
).replace("protection times must cover the activity window", "protection times must contain task_time")
