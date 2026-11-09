"""Validate local completion schedules without choosing their departure time."""
import re
from task_module.task_types.itinerary.clock_precision import clock_minutes


def minutes(value):
    return clock_minutes(value)


def validate_transition(result, context):
    try:
        if set(result) != {'route_ref', 'departure_time', 'activity_start', 'before_departure', 'after_arrival'}:
            raise ValueError('return exactly the specified transition fields')
        route = context['route']
        if result['route_ref'] != route['route_ref']:
            raise ValueError('route_ref must reference the supplied factual route')
        now = minutes(context['current_time'])
        departure = minutes(result['departure_time'])
        arrival = departure + route['duration_minutes']
        start = minutes(result['activity_start'])
        if not now <= departure <= arrival <= start < minutes(context['latest_activity_end']):
            raise ValueError('departure/arrival/activity start violate chronological or fixed-boundary limits')
        ends = [min(int(closed), minutes(context['latest_activity_end']))
                for opened, closed in context['opening_intervals'] if int(opened) <= start < int(closed)]
        if not ends or max(ends) <= start:
            raise ValueError('POI activity must start during an open interval with time remaining')
        for key, begin, end in [('before_departure', now, departure), ('after_arrival', arrival, start)]:
            blocks = result[key]
            if not isinstance(blocks, list):
                raise ValueError(f'{key} must be a list')
            cursor = begin
            for block in blocks:
                if not isinstance(block, dict) or set(block) != {'start', 'end', 'activity_name'}:
                    raise ValueError(f'{key} blocks require start, end, activity_name only')
                if not isinstance(block['activity_name'], str) or not block['activity_name'].strip():
                    raise ValueError('personal activity_name must be nonempty')
                if minutes(block['start']) != cursor or not cursor < minutes(block['end']) <= end:
                    raise ValueError(f'{key} must cover its interval continuously without overlap')
                cursor = minutes(block['end'])
            if cursor != end:
                raise ValueError(f'{key} leaves an uncovered interval')
        return ''
    except (ValueError, TypeError, KeyError) as exc:
        return str(exc)
