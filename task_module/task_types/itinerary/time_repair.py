"""Conditional, bounded repair of a completion result. Final Writer is not involved."""
from copy import deepcopy
import json
import math
import re

from task_module.task_types.itinerary.clock_precision import clock_seconds


SYSTEM = """You repair one time boundary and the associated existing local completion result.
Propose a natural, explicit boundary that includes the target route and excludes the supplied competitors.
Return JSON with exactly boundary and transition. boundary has text, minimum_seconds,
maximum_seconds, minimum_inclusive, maximum_inclusive. Use 0 as the lower bound when unnecessary.
transition has exactly the same structure as original_transition. Only the listed editable_time_fields
may change, using HH:mm or HH:mm:ss. Keep every other value and list length unchanged.
Make the local travel interval consistent with the boundary and at least the actual target duration.
Do not choose places, change other constraints, add activities, or reschedule unrelated nodes.
After validation feedback, return the complete corrected object, not a patch.
"""


class TimeRepairError(RuntimeError):
    pass


def integer_bounds(low, high, low_closed=True, high_closed=True):
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in (low, high)) or low < 0 or high < low:
        raise ValueError('invalid numerical boundary')
    return (math.ceil(low) if low_closed else math.floor(low) + 1,
            math.floor(high) if high_closed else math.ceil(high) - 1)


def text_bounds(text):
    """Read common explicit English/Chinese duration comparisons, including open bounds."""
    value = text.lower()
    words = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split()
    for number, word in reversed(list(enumerate(words))):
        value = re.sub(r'\b' + word + r'\b', str(number), value)
    value = re.sub(r'(\d+)\s+and\s+(?:a|1)\s+half', lambda m: str(int(m[1]) + .5), value)
    value = re.sub(r'(\d+)\s*分(?:钟)?半', lambda m: str(int(m[1]) + .5) + '分', value)
    value = re.sub(r'(?<=\d)[-–](?=minute|second)', ' ', value)
    duration = r'(\d+(?:\.\d+)?)\s*(minutes?|mins?\b|分钟|分)(?:\s*(?:and\s+)?(\d+(?:\.\d+)?)\s*(?:seconds?|secs?\b|秒))?'
    value = re.sub(duration, lambda m: f'{float(m[1])*60+float(m[3] or 0):g}s', value)
    value = re.sub(r'(\d+(?:\.\d+)?)\s*(?:seconds?|secs?\b|秒)', r'\1s', value)
    between = re.search(r'(?:between|介于)\s*(\d+(?:\.\d+)?)s\s*(?:and|和|与|至|到)\s*(\d+(?:\.\d+)?)s', value)
    if between:
        return integer_bounds(float(between[1]), float(between[2]))
    low, high, lc, hc = 0, None, True, True
    comparisons = r'(no more than|not more than|at most|within|up to|at least|no less than|more than|greater than|less than|不超过|至多|至少|大于|超过|小于)\s*(?:a\s+)?(\d+(?:\.\d+)?)s'
    for m in re.finditer(comparisons, value):
        op, seconds = m[1], float(m[2])
        if op in {'at least', 'no less than', '至少', 'more than', 'greater than', '大于', '超过'}:
            low, lc = seconds, op in {'at least', 'no less than', '至少'}
        else:
            high, hc = seconds, op not in {'less than', '小于'}
    if high is None:
        m = re.search(r'(\d+(?:\.\d+)?)s以内', value)
        if m:
            high = float(m[1])
    if high is None:
        raise ValueError('cannot recognize an explicit upper bound in boundary.text')
    return integer_bounds(low, high, lc, hc)


def editable_fields(transition):
    paths = ['departure_time', 'activity_start']
    for kind in ('before_departure', 'after_arrival'):
        for index in range(len(transition[kind])):
            paths.extend([f'{kind}/{index}/start', f'{kind}/{index}/end'])
    return paths


def validate_response(result, payload, private_check):
    if not isinstance(result, dict) or set(result) != {'boundary', 'transition'}:
        raise ValueError('return exactly boundary and transition')
    b = result['boundary']
    if not isinstance(b, dict) or set(b) != {'text', 'minimum_seconds', 'maximum_seconds', 'minimum_inclusive', 'maximum_inclusive'}:
        raise ValueError('boundary fields are missing or unexpected')
    if not isinstance(b['text'], str) or not all(isinstance(b[k], bool) for k in ('minimum_inclusive', 'maximum_inclusive')):
        raise ValueError('boundary.text must be text and inclusivity fields must be booleans')
    bounds = integer_bounds(b['minimum_seconds'], b['maximum_seconds'], b['minimum_inclusive'], b['maximum_inclusive'])
    if bounds != text_bounds(b['text']):
        raise ValueError(f'boundary text normalizes to {text_bounds(b["text"])}, structured fields normalize to {bounds}')
    low, high = bounds
    if not low <= payload['target_seconds'] <= high:
        raise ValueError(f'boundary {bounds} excludes target {payload["target_seconds"]} seconds')
    admitted = [v for v in payload['competitor_seconds'] if low <= v <= high]
    if admitted:
        raise ValueError(f'boundary {bounds} admits competing durations {admitted}')
    original = payload['original_transition']
    corrected = result['transition']
    # Replace authorized scalar times in a copy, then compare entire structure.
    expected = deepcopy(original)
    for path in payload['editable_time_fields']:
        source, dest = corrected, expected
        parts = path.split('/')
        for part in parts[:-1]:
            key = int(part) if isinstance(source, list) else part
            source, dest = source[key], dest[key]
        clock_seconds(source[parts[-1]])
        dest[parts[-1]] = source[parts[-1]]
    if expected != corrected:
        raise ValueError('transition changed a protected field or list structure')
    private_check(corrected, bounds)
    return bounds


def repair_time(client, payload, private_check, *, audit=None, attempts=2):
    """Append failed complete responses and precise feedback; never reset a retry conversation."""
    if client is None:
        raise TimeRepairError('time repair requires a configured construction Agent')
    messages = [{'role': 'system', 'content': SYSTEM},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
    errors = []
    for attempt in range(attempts):
        options = {'temperature': .4, 'response_format': {'type': 'json_object'}}
        call = audit.start_call('time_repair', client.model, client.endpoint_name,
                                messages, options, secret=client.api_key, role_attempt=attempt+1) if audit else None
        try:
            raw = client.call_chat(messages, extra=options)
            if call:
                call.received(raw, client.last_exchange)
        except Exception as exc:
            if call:
                call.fail(exc, 'transport', client.last_exchange)
            raise
        content = ((raw.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        messages.append({'role': 'assistant', 'content': content})
        try:
            result = json.loads(content)
            bounds = validate_response(result, payload, private_check)
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            feedback = {'field': 'boundary_or_transition', 'reason': str(exc),
                        'instruction': 'Correct this issue and return the complete boundary and transition.'}
            errors.append(feedback)
            if call:
                call.finish('rejected', validation_errors=[feedback])
            messages.append({'role': 'user', 'content': json.dumps(feedback, ensure_ascii=False)})
            continue
        if call:
            call.finish('completed', normalized_bounds_seconds=list(bounds))
        return result, bounds
    raise TimeRepairError('time repair attempts exhausted: ' + json.dumps(errors, ensure_ascii=False))
