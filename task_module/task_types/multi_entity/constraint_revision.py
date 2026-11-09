"""Author-side C/prose revision with append feedback and candidate-local proof."""
from copy import deepcopy
import json
import math
import re

from shared.llm_client import OpenAICompatibleChatClient
from shared.config_loader import PipelineConfig
from .choice_verification import verify_candidate_choice
from .natural_prompt import validate_natural_user_prompt
from .constraint_protocol import validate_constraints
from .time_options import schedule_options

SYSTEM = """You are a multi-entity task constructor. Using the supplied frozen place and route
facts, revise the task's structured key constraints C around the fixed target place and write
a corresponding natural English user request, so that only the target is feasible among this
task's candidates. Preserve participant identities, departure locations, activity goal, place
type, independent arrival, and shared activity duration. Keep original participant
time windows unless you select a supplied schedule option. Copy that option's
participant times, accompanying conditions and commute preference together.
Add fact-supported constraints to distinguish the fixed target from the remaining feasible
competitors. Revise only your additions after feedback; do not rewrite original C or facts.
Do not invent budget, capacity, dietary, opening, or other place attributes to manufacture
uniqueness. Preserve the original request as much as possible, making only necessary natural
revisions. Do not directly specify the target, list candidates, leak internal fields, or compare
answers in the request. Express all requirements used in computation consistently in both
structured C and the request. After feedback, return a complete revision, not just a patch.
Write user_prompt in English even when the original request is Chinese; preserve supplied
participant names and departure-area values in the structured fields.
State available_from as when the person can first leave their own starting point.
State must_leave_by as when they must leave the shared venue.
When an activity_date is supplied, include that exact date in the request.
Candidate opening intervals are local minutes since midnight on that date.
The full shared activity must fit within one opening interval.
When a departure-area value is the person's own name, refer to their own
starting point naturally in the request. Keep the exact value in structured C.
Return JSON with this structure (keep original values where indicated):
{"public_fact_projection": {"participants": [{"name": "<original name>",
"departure_area": "<original departure area>", "available_from": "HH:MM", "must_leave_by": "HH:MM"}],
"activity": {"activity_type": "<original activity>", "required_place_type": "<original type>",
"duration_min": "<positive integer minutes>", "arrival_mode": "independent"},
"additional_constraints": [{"object": "<subject>", "attribute": "<property>",
"relation": "<comparison>", "value": "<typed value>"}]},
"user_prompt": "<complete English request>"}.
duration_min must be a JSON integer, not a string.
If the initial verification already uniquely preserves the target, write the
original requirements without adding constraints or an early arrival.
Otherwise public_fact_projection may include early_arrival, an optional object
with exactly these fields:
- participants: one or more distinct exact names from the supplied participant list.
- lead_minutes: a positive JSON number. Each listed person must arrive at least
  this many minutes before the shared activity starts. Travel and shared activity
  duration are measured separately.
- purpose: the specific task these people will do at the venue before the shared
  activity. Use a venue service only when the supplied facts support it.
Choose the people, lead time and task together. State all three in the user
request. Omit early_arrival if no such arrangement is selected.
Copy commute_preference only from a supplied schedule option. Its participant
and mode identify whose one-way commute is compared and how they travel.
With target_minutes, prefer the route duration closest to that number. Without
it, prefer the shortest duration. Express the preference after the hard requirements.
Route minutes may include fractional minutes derived from frozen seconds.
Put other additions in additional_constraints; do not invent activity field names. Each addition
uses object, attribute, relation, value. Supported computations:
- object=activity, attribute=start_time, relation=le/ge/eq, value=HH:MM local clock.
- object=all_participants or an exact participant name, attribute=commute_min,
  relation=le/ge/eq, value=a non-negative number of one-way travel minutes.
- object=all_participants or an exact participant name, attribute=travel_mode,
  relation=in, value=a non-empty list drawn from WALK, TRANSIT, DRIVE.
- object=place, attribute=google_types, relation=contains, value=a supplied Google type string.
  This membership uses the complete frozen type list, independently of the original primary type.
le means at most/no later than; ge means at least/no earlier than; eq means equal.
These definitions describe the output protocol, not which condition to add. Independently
choose fact-supported requirements. Departure scheduling, waiting restrictions, and other
unlisted properties do not currently have computation support. Missing route evidence is
not proof of an unavailable route. All added constraints are jointly required.
"""


def configured_client(config_yaml, stage='scenario_patch'):
    config = PipelineConfig(config_yaml)
    name = config.stage_model(stage)
    endpoint = config.model_endpoint(name)
    return OpenAICompatibleChatClient(endpoint['base_url'], endpoint['api_key'], endpoint['model'],
                                      endpoint_name=name)


def validate_revision(result, original, venues, routes, target, *, allow_legacy_additions=False,
                      options=(), original_is_unique=False, activity_date=None):
    facts = result['public_fact_projection']
    if set(facts) - {'participants', 'activity', 'additional_constraints', 'early_arrival', 'commute_preference'} or len(facts['participants']) != len(original['participants']):
        raise ValueError('Participant count or public C object schema differs from the original')
    added = set(facts['activity']) - set(original['activity'])
    if added and not allow_legacy_additions:
        raise ValueError('Activity fields added outside additional_constraints: ' + ', '.join(sorted(added)))
    if added - {'allowed_travel_modes', 'max_commute_min'}:
        raise ValueError('Unsupported activity fields: ' + ', '.join(sorted(added - {'allowed_travel_modes', 'max_commute_min'})))
    changes = [{'field': 'activity.' + k, 'expected': v, 'actual': facts['activity'].get(k)}
               for k, v in original['activity'].items() if facts['activity'].get(k) != v]
    if changes:
        raise ValueError('Original activity C changed: ' + json.dumps(changes, ensure_ascii=False))
    modes = facts['activity'].get('allowed_travel_modes')
    if modes is not None and (not isinstance(modes, list) or not modes or any(m not in ('WALK', 'TRANSIT', 'DRIVE') for m in modes)):
        raise ValueError('allowed_travel_modes has invalid type or enum')
    cap = facts['activity'].get('max_commute_min')
    if cap is not None and (type(cap) not in (int, float) or not math.isfinite(cap) or cap < 0):
        raise ValueError('max_commute_min must be a finite non-negative number')
    duration = facts['activity']['duration_min']
    validate_constraints(facts.get('additional_constraints', []), facts['participants'])
    old_additions = original.get('additional_constraints', [])
    final_additions = facts.get('additional_constraints', [])
    if original_is_unique and (final_additions != old_additions or 'early_arrival' in facts):
        raise ValueError('The original C already uniquely preserves the target; do not add conditions')
    if facts['participants'] != original['participants'] or 'commute_preference' in facts:
        if not any(facts['participants'] == option['participants']
                   and facts.get('commute_preference') == option.get('commute_preference')
                   and all(c in final_additions for c in option.get('additional_constraints', []))
                   for option in options):
            raise ValueError('Original participant C changed or commute preference supplied without a complete schedule option')
    if any(c not in facts.get('additional_constraints', []) for c in old_additions):
        raise ValueError('Original additional C removed or changed')
    if type(duration) is not int or duration <= 0 or duration >= 1440:
        raise ValueError('duration_min must be positive minutes within one day')
    for person in facts['participants']:
        for field in ('available_from', 'must_leave_by'):
            if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', str(person[field])):
                raise ValueError(f'Invalid clock: {field}')
        if person['available_from'] >= person['must_leave_by']:
            raise ValueError('Participant time window must be positive')
    prompt = result.get('user_prompt')
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('user_prompt is missing or empty')
    if activity_date and activity_date not in prompt:
        raise ValueError(f'Include the bound local activity date {activity_date} in the request')
    errors = validate_natural_user_prompt(prompt, [])
    if errors:
        raise ValueError('; '.join(errors))
    if any(v['name'] in prompt for v in venues):
        raise ValueError('user_prompt contains a private candidate name')
    return verify_candidate_choice(facts, venues, routes, target)


def revise_constraints(client, original, venues, routes, target, *, original_prompt='', attempts=3, activity_date=None,
                       save_audit=lambda record: None):
    target_name = next(v['name'] for v in venues if v['place_id'] == target)
    baseline = verify_candidate_choice(original, venues, routes, target)
    options = schedule_options(original, venues, routes, target, baseline)
    relevant_ids = {target} | set(baseline['accepted_place_ids']) | set(baseline['unresolved_place_ids'])
    relevant = [v for v in venues if v['place_id'] in relevant_ids]
    route_keys = {f"{p['departure_area']}→{v['name']}" for p in original['participants'] for v in relevant}
    context = {'original_C': original, 'original_prompt': original_prompt, 'fixed_target': target_name,
               'candidate_facts': [{'name': v['name'], 'primary_type': v.get('primary_type'),
                                    'google_types': v.get('types', [])} for v in relevant],
               'route_minutes': {k: v for k, v in routes.items() if k in route_keys},
               'schedule_options': options,
               'original_is_unique': baseline['valid'],
               'initial_verification': [r for r in baseline['candidate_results'] if r['place_id'] in relevant_ids]}
    if activity_date:
        context['activity_date'] = activity_date
        for item, venue in zip(context['candidate_facts'], relevant):
            item['opening_intervals_local_minutes'] = venue.get('activity_opening_intervals')
    messages = [{'role': 'system', 'content': SYSTEM},
                {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}]
    log = {'model': client.model, 'context': context, 'attempts': []}
    for index in range(attempts):
        entry = {'attempt': index + 1, 'messages': deepcopy(messages), 'status': 'started'}
        log['attempts'].append(entry)
        save_audit(log)
        try:
            raw = client.call_chat(messages, extra={'temperature': .4, 'response_format': {'type': 'json_object'}})
        except Exception as error:
            entry.update(status='infrastructure_failure', error_type=type(error).__name__)
            save_audit(log)
            raise
        entry['raw_response'] = raw
        content = raw['choices'][0]['message']['content']
        messages.append({'role': 'assistant', 'content': content})
        try:
            result = json.loads(content)
            proof = validate_revision(result, original, venues, routes, target,
                                      options=options, original_is_unique=baseline['valid'],
                                      activity_date=activity_date)
            entry.update(result=result, verification=proof)
            if proof['valid']:
                entry['status'] = 'verified_draft'
                save_audit(log)
                return result, proof, log
            feedback = {'problem': 'C does not uniquely preserve the fixed GT',
                        'candidate_results': [r for r in proof['candidate_results'] if r['place_id'] in relevant_ids]}
        except (ValueError, KeyError, TypeError) as error:
            feedback = {'problem': str(error)}
        entry.update(status='rejected', feedback=feedback)
        messages.append({'role': 'user', 'content': json.dumps(feedback, ensure_ascii=False)})
        save_audit(log)
    return None, None, log
