from __future__ import annotations
import math
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

def _stage3_map_details(place: dict[str, Any]) -> dict[str, Any]:
    projection = place.get('map_projection') if isinstance(place.get('map_projection'), dict) else {}
    details = projection.get('google_place_details') if isinstance(projection.get('google_place_details'), dict) else {}
    return details

def _stage3_price_fact(place: dict[str, Any]) -> tuple[Decimal | None, Decimal | None, str]:
    """Read one already-frozen Google price fact; never request or infer it."""
    price_range = _stage3_map_details(place).get('price_range')
    if not isinstance(price_range, dict):
        return (None, None, '')

    def money(key: str) -> Decimal | None:
        value = price_range.get(key)
        if not isinstance(value, dict):
            return None
        try:
            units = Decimal(str(value.get('units') or 0))
            nanos = Decimal(str(value.get('nanos') or 0)) / Decimal('1000000000')
            return units + nanos
        except (InvalidOperation, TypeError, ValueError):
            return None
    start = price_range.get('startPrice') if isinstance(price_range.get('startPrice'), dict) else {}
    end = price_range.get('endPrice') if isinstance(price_range.get('endPrice'), dict) else {}
    start_currency = str(start.get('currencyCode') or '').upper()
    end_currency = str(end.get('currencyCode') or '').upper()
    if not start_currency or start_currency != end_currency:
        return (None, None, '')
    return (money('startPrice'), money('endPrice'), start_currency)

def _stage3_route_index(route_cache: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Index only available static routes from a frozen cache artifact."""
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in route_cache.get('cache') or []:
        if not isinstance(entry, dict) or str(entry.get('snapshot_id') or 'static_v1') != 'static_v1':
            continue
        route = entry.get('route') if isinstance(entry.get('route'), dict) else {}
        if route.get('available') is not True or int(route.get('duration_s') or 0) <= 0:
            continue
        key = (str(entry.get('origin_place_id') or ''), str(entry.get('dest_place_id') or ''), str(entry.get('mode') or '').upper())
        result[key] = entry
    return result

def _stage3_route_for(route_index: dict[tuple[str, str, str], dict[str, Any]], character: dict[str, Any], place: dict[str, Any], mode: str) -> dict[str, Any] | None:
    key = (f"character:{character.get('character_id')}", str(place.get('place_id') or ''), mode)
    entry = route_index.get(key)
    if entry is None:
        return None
    return entry

def _regular_opening_hours(place: dict[str, Any]) -> dict[str, Any] | None:
    """Read effective frozen world hours without deriving a task time."""
    from shared.opening_hours import effective_opening_hours
    return effective_opening_hours(place)

def _region_district_names(world: dict[str, Any]) -> dict[str, str]:
    return {str(item.get('district_id') or ''): str(item.get('name') or '') for item in world.get('districts') or [] if isinstance(item, dict) and item.get('district_id') and item.get('name')}

def _region_place_district(place: dict[str, Any], district_names: dict[str, str]) -> str:
    return str(place.get('district_name') or district_names.get(str(place.get('district_id') or '')) or '')

def _region_weekday_index(time_context: dict[str, Any]) -> int:
    value = str(time_context.get('instruction_time') or '')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return (parsed.weekday() + 1) % 7

def _region_opening_intervals(place: dict[str, Any], weekday_index: int) -> list[tuple[int, int]]:
    hours = _regular_opening_hours(place) or {}
    result: list[tuple[int, int]] = []
    for period in hours.get('periods') or []:
        if not isinstance(period, dict):
            continue
        opening = period.get('open') if isinstance(period.get('open'), dict) else {}
        closing = period.get('close') if isinstance(period.get('close'), dict) else {}
        if int(opening.get('day', -1)) != weekday_index:
            continue
        start = int(opening.get('hour') or 0) * 60 + int(opening.get('minute') or 0)
        close_day = int(closing.get('day', weekday_index))
        end = int(closing.get('hour') or 0) * 60 + int(closing.get('minute') or 0)
        if close_day != weekday_index or end <= start:
            end += 24 * 60
        result.append((start, end))
    return result

def _region_price_contains(place: dict[str, Any], amount: Decimal, currency: str) -> bool:
    (minimum, maximum, place_currency) = _stage3_price_fact(place)
    return minimum is not None and maximum is not None and (place_currency == currency) and (minimum <= amount <= maximum)

def _region_constraint_matches_place(place: dict[str, Any], constraint: dict[str, Any], *, character: dict[str, Any], route_index: dict[tuple[str, str, str], dict[str, Any]], district_names: dict[str, str], weekday_index: int) -> bool:
    kind = str(constraint.get('type') or '')
    if kind == 'primary_type':
        return str(place.get('primary_type') or '') == str(constraint.get('value') or '')
    if kind == 'district_name':
        return _region_place_district(place, district_names) == str(constraint.get('value') or '')
    if kind == 'service_feature':
        return _stage3_map_details(place).get(str(constraint.get('value') or '')) is True
    if kind == 'price_maximum':
        (_minimum, maximum, currency) = _stage3_price_fact(place)
        return currency == str(constraint.get('unit') or '').upper() and maximum is not None and (maximum <= Decimal(str(constraint['value'])))
    if kind == 'price_target_amount':
        return _region_price_contains(place, Decimal(str(constraint['value'])), str(constraint.get('unit') or '').upper())
    if kind in {'opens_by', 'closes_at_or_after'}:
        intervals = _region_opening_intervals(place, weekday_index)
        if not intervals:
            return False
        value = int(constraint['value'])
        return min((start for (start, _end) in intervals)) <= value if kind == 'opens_by' else max((end for (_start, end) in intervals)) >= value
    if kind in {'route_duration_range', 'walking_distance_range'}:
        mode = str(constraint.get('transportation_mode') or '').upper()
        entry = _stage3_route_for(route_index, character, place, mode)
        if entry is None:
            return False
        route = entry.get('route') or {}
        if kind == 'route_duration_range':
            raw = route.get('duration_s')
            value = max(1, math.ceil(float(raw) / 60)) if raw is not None else None
        else:
            raw = route.get('distance_m') if mode == 'WALK' else route.get('walking_distance_m')
            value = int(raw) if raw is not None else None
        return value is not None and int(constraint['minimum']) <= value <= int(constraint['maximum'])
    return False
