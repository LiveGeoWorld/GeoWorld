"""Shared geospatial helpers for runtime apps.

Pure functions used by :class:`MapAppRuntime` (and the discovery executor) to
project world objects into agent-facing payloads, normalise free-text queries,
score keyword matches, and compute great-circle distances.
"""
from __future__ import annotations
import math
import unicodedata
from difflib import SequenceMatcher
from typing import Any

def distance_m(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Great-circle distance between two ``{lat, lng}`` points, in metres."""
    if not a or not b:
        return 0.0
    (lat1, lng1) = (math.radians(float(a.get('lat', 0))), math.radians(float(a.get('lng', 0))))
    (lat2, lng2) = (math.radians(float(b.get('lat', 0))), math.radians(float(b.get('lng', 0))))
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 6371000 * 2 * math.atan2(math.sqrt(h), math.sqrt(1 - h))

def norm(value: Any) -> str:
    return unicodedata.normalize('NFKC', str(value or '')).casefold().replace(' ', '').replace('\u3000', '')

def norm_query(value: Any) -> str:
    return norm(str(value or '')).replace('/', '').replace(',', '').replace('，', '').replace('_', '').replace('-', '').replace('–', '').replace('—', '')

def factual_place_aliases(place: dict[str, Any]) -> list[str]:
    """Return only aliases explicitly stored on the frozen place record."""
    values: list[Any] = []
    for source in (place, place.get('map_projection', {})):
        for key in ('aliases', 'alternate_names', 'alternative_names'):
            value = source.get(key)
            values.extend(value if isinstance(value, list) else [value])
        for key in ('name_en', 'english_name'):
            values.append(source.get(key))
    return list(dict.fromkeys((str(value).strip() for value in values if str(value or '').strip())))

def bounded_name_similarity(query: str, candidate_name: str) -> float:
    """Conservatively compare normalized place names for typo fallback."""
    left = norm_query(query)
    right = norm_query(candidate_name)
    if min(len(left), len(right)) < 4:
        return 0.0
    similarity = SequenceMatcher(None, left, right, autojunk=False).ratio()
    for suffix in ('北门', '南门', '东门', '西门', '入口', '出口', 'northgate', 'southgate', 'eastgate', 'westgate', 'entrance', 'exit'):
        if right.endswith(suffix):
            core = right[:-len(suffix)]
            if len(core) >= 4 and (core in left or left in core):
                similarity = max(similarity, 0.95)
    return similarity

def query_terms(value: str) -> list[str]:
    raw = str(value or '')
    terms = [raw]
    for marker in ['公园', '医院', '宾馆', '酒店', '东湖', '人民路', '老城厢', '豫园', '陆家嘴', '南京路']:
        if marker in raw:
            terms.append(marker)
    return [norm_query(term) for term in terms if term]

def place_payload(place: dict[str, Any]) -> dict[str, Any]:
    projection = place.get('map_projection', {})
    return {'place_id': place.get('place_id', ''), 'name': place.get('name', ''), 'location': place.get('location', {}), 'type': place.get('primary_type', ''), 'rating': projection.get('rating'), 'address': place.get('address', ''), 'city_id': place.get('city_id', ''), 'district_id': place.get('district_id', '')}

def service_payload(service: dict[str, Any], place: dict[str, Any] | None) -> dict[str, Any]:
    """Return public service facts without leaking frozen-world storage IDs."""
    state = service.get('state_model', {})
    payload = place_payload(place or {})
    return {'name': payload.get('name') or 'Unnamed service', 'location': payload.get('location', {}), 'service_type': service.get('service_type', ''), 'rating': payload.get('rating'), 'city': payload.get('city_id', ''), 'price_band': state.get('price_band', ''), 'queue_time_min': state.get('queue_time_min'), 'crowding_level': state.get('crowding_level', ''), 'address': payload.get('address', '')}

def grounding_match_score(candidate: dict[str, Any], parts: list[str]) -> int:
    if not parts:
        return 1
    haystack = candidate.get('_search_text', '')
    score = 0
    for part in parts:
        if part and part in haystack:
            score += 3
        for term in query_terms(part):
            if term and term in haystack:
                score += 1
    return score
FIELD_LABELS_ZH = {'name': '名称', 'address': '地址', 'district': '区域', 'category': '类别', 'city': '城市'}

def grounding_match_detail(candidate: dict[str, Any], parts: list[str]) -> tuple[int, list[str]]:
    """Return ``(score, matched_fields)`` for a candidate against query parts.

    The matched fields are restricted to *map metadata* only (name / address /
    district / category / city). This explains *why a candidate was recalled* —
    never why it visually matches an image, which the agent alone must judge.
    """
    score = grounding_match_score(candidate, parts)
    fields = candidate.get('_fields', {})
    matched: list[str] = []
    for (field_name, field_text) in fields.items():
        if not field_text:
            continue
        for part in parts:
            if part and part in field_text:
                matched.append(field_name)
                break
            if any((term and term in field_text for term in query_terms(part))):
                matched.append(field_name)
                break
    return (score, matched)

def explain_match(matched_fields: list[str], distance_to_anchor_m: int | None=None) -> str:
    reasons = [FIELD_LABELS_ZH.get(field, field) for field in matched_fields]
    if distance_to_anchor_m is not None:
        reasons.append(f'距锚点约{distance_to_anchor_m}米')
    if not reasons:
        return '兜底召回（无字段匹配）'
    return '匹配字段：' + '、'.join(dict.fromkeys(reasons))

def public_grounding_candidate(candidate: dict[str, Any] | None) -> dict[str, Any]:
    if not candidate:
        return {}
    return {'candidate_id': candidate.get('candidate_id', ''), 'name': candidate.get('name', ''), 'location': candidate.get('location', {}), 'city_id': candidate.get('city_id', ''), 'city_name': candidate.get('city_name', ''), 'district_id': candidate.get('district_id', ''), 'address': candidate.get('address', ''), 'tags': candidate.get('tags', [])}

def grounding_candidate(*, candidate_id: str, name: str, location: dict[str, Any], district_id: str, city_id: str, city_name: str, address: str, tags: list[str], aliases: list[str], fuzzy_aliases: list[str] | None=None) -> dict[str, Any]:
    search_text = norm_query(' '.join((str(part) for part in [name, city_id, city_name, district_id, address, *tags, *aliases] if part)))
    fields = {'name': norm_query(name), 'alias': norm_query(' '.join((str(alias) for alias in aliases if alias))), 'address': norm_query(address), 'district': norm_query(district_id), 'category': norm_query(' '.join((str(tag) for tag in tags if tag))), 'city': norm_query(f'{city_id} {city_name}')}
    return {'candidate_id': candidate_id, 'name': name, 'location': location, 'city_id': city_id, 'city_name': city_name, 'district_id': district_id, 'address': address, 'tags': [tag for tag in tags if tag], '_search_text': search_text, '_fields': fields, '_aliases': [alias for alias in fuzzy_aliases or [] if alias and norm_query(alias) != norm_query(name)]}

def grounding_candidate_from_place(place: dict[str, Any], city_name: str='') -> dict[str, Any]:
    payload = place_payload(place)
    explicit_aliases = factual_place_aliases(place)
    aliases = [payload.get('name', ''), payload.get('address', ''), payload.get('city_id', ''), city_name, payload.get('district_id', ''), payload.get('type', ''), *place.get('map_projection', {}).get('types', []), *explicit_aliases]
    return grounding_candidate(candidate_id=payload.get('place_id', ''), name=payload.get('name', ''), location=payload.get('location', {}), district_id=payload.get('district_id', ''), city_id=payload.get('city_id', ''), city_name=city_name, address=payload.get('address', ''), tags=[payload.get('type', '')], aliases=aliases, fuzzy_aliases=explicit_aliases)

def grounding_candidate_from_character(character: dict[str, Any]) -> dict[str, Any] | None:
    mobility = character.get('mobility', {})
    if 'lat' not in mobility or 'lng' not in mobility:
        return None
    name = '地图候选位置'
    aliases = [name, character.get('name', ''), character.get('occupation', ''), mobility.get('home_district_name', ''), mobility.get('work_district_name', '')]
    return grounding_candidate(candidate_id=f"map_location:{character.get('character_id', '')}", name=name, location={'lat': mobility.get('lat'), 'lng': mobility.get('lng')}, district_id=mobility.get('work_district_id') or mobility.get('home_district_id', ''), city_id='', city_name='', address=mobility.get('work_district_name') or mobility.get('home_district_name', ''), tags=['candidate_location'], aliases=aliases)
