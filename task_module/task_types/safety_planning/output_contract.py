"""Public Safety submission contract; no discovery-tool hints or private IDs."""

SAFETY_OUTPUT_CONTRACT = '''

SAFETY TASK OUTPUT CONTRACT

When you are ready to finish, call `UserInterfaceApp__final_answer` with exactly this structure:

{
  "feasible": true,
  "plan": {
    "selection": {
      "name": "Exact place name returned by the corresponding tool",
      "location": {"lat": 0.0, "lng": 0.0}
    },
    "safety_information": ["Exact complete description text copied from a relevant safety notice in a tool result."]
  },
  "explanation": "A natural-language answer explaining your choice and how the safety information affects it."
}

Output rules:

- Select a place that satisfies the user request, including the relevant safety conditions.
- Copy name, location.lat, and location.lng exactly from the corresponding tool result.
- Each safety_information entry must be an exact, complete copy of the description text of relevant information actually returned by a tool during this task. The submitted text must match the tool-returned content character for character. JSON escaping is allowed; the decoded string must remain identical.
- In explanation, explain the choice naturally and how the observed safety information affects it.
- If no place can satisfy all requirements, set feasible to false, set plan.selection to null, retain the safety information you obtained, and explain the blocking requirement.
'''
