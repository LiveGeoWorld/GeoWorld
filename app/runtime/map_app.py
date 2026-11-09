"""MapAppRuntime — executable MapApp for grounding & discovery tasks.

A neutral, global map index built from the merged Bench world (multiple cities).
It does *not* know the gold answer; it only filters and ranks the same public
place/candidate records any other query would see. Grounding works like a
multiple-choice lookup: the agent searches candidates, then commits a choice via
``select_grounded_location`` (a WRITE operation that decides task success).
"""
from __future__ import annotations
from typing import Any
from app.runtime.base import OperationType, RuntimeApp, app_tool
from app.runtime.geo import distance_m, bounded_name_similarity, explain_match, grounding_candidate_from_character, grounding_candidate_from_place, grounding_match_detail, norm_query, place_payload, public_grounding_candidate
_FAR = 10 ** 9

class MapAppRuntime(RuntimeApp):
    """Deterministic MapApp backed by merged world places + map candidates."""
    name = 'MapApp'
    # Complete category queries only; do not rewrite parts of place names.
    _CATEGORY_QUERY_ALIASES = {'busstop': 'busstation'}

    def __init__(self, *, places: list[dict[str, Any]], characters: list[dict[str, Any]] | None=None, cities: list[dict[str, Any]] | None=None) -> None:
        self.places = places or []
        self.characters = characters or []
        self.city_names = {city.get('city_id', ''): city.get('name', '') for city in cities or [] if city.get('city_id')}
        self.grounding_candidates = self._build_grounding_candidates()
        super().__init__()

    @app_tool(OperationType.READ, summary='搜索地点/商户')
    def search_places(self, args: dict[str, Any]) -> dict[str, Any]:
        keyword = str(args.get('keyword', '')).lower()
        city_id = str(args.get('city_id', '') or '')
        city_hint = norm_query(args.get('city_hint', ''))
        district_id = args.get('district_id', '')
        place_type = args.get('place_type', '')
        limit = int(args.get('limit', 5) or 5)
        matches = []
        for place in self.places:
            haystack = ' '.join((str(part or '') for part in [place.get('name', ''), place.get('address', ''), place.get('city_id', ''), self.city_names.get(place.get('city_id', ''), ''), place.get('district_id', ''), ' '.join((str(t or '') for t in place.get('map_projection', {}).get('types', [])))])).lower()
            if keyword and (not any((part.lower() in haystack for part in keyword.replace('/', ' ').split()))):
                continue
            if city_id and place.get('city_id') != city_id:
                continue
            if city_hint and city_hint not in norm_query(f"{place.get('city_id', '')} {self.city_names.get(place.get('city_id', ''), '')}"):
                continue
            if district_id and place.get('district_id') != district_id:
                continue
            if place_type and place.get('primary_type') != place_type:
                continue
            payload = place_payload(place)
            if str(place.get("place_id", "")).startswith("district_anchor:") and norm_query(keyword) == norm_query(place.get("name")):
                matches.insert(0, payload)
            else:
                matches.append(payload)
        identified = matches[0].get('location', {}) if matches else {}
        return {'places': matches[:limit], 'identified_location': identified}

    @app_tool(OperationType.READ, summary='搜索 grounding 候选位置')
    def search_grounding_candidates(self, args: dict[str, Any]) -> dict[str, Any]:
        keyword = norm_query(args.get('keyword', ''))
        keyword = self._CATEGORY_QUERY_ALIASES.get(keyword, keyword)
        area_hint = norm_query(args.get('area_hint', ''))
        entity_type_hint = norm_query(args.get('entity_type_hint', ''))
        category = norm_query(args.get('category', ''))
        category = self._CATEGORY_QUERY_ALIASES.get(category, category)
        district_id = norm_query(args.get('district_id', ''))
        address_hint = norm_query(args.get('address_hint', ''))
        near = str(args.get('near', '') or '')
        near_candidate_id = str(args.get('near_candidate_id', '') or '')
        near_radius_m = int(args.get('near_radius_m', 0) or 0)
        city_id = str(args.get('city_id', '') or '')
        city_hint = norm_query(args.get('city_hint', ''))
        limit = int(args.get('limit', 8) or 8)
        blob_parts = [part for part in [keyword, area_hint, entity_type_hint] if part]
        field_queries = [(field, term) for (field, term) in (('category', category), ('district', district_id), ('address', address_hint)) if term]
        has_query = bool(blob_parts or field_queries)
        anchor = self._candidate_by_id(near_candidate_id) if near_candidate_id else None
        if anchor is None and near:
            anchor = self._resolve_anchor(near, city_id, city_hint)
        anchor_loc = anchor.get('location', {}) if anchor else {}
        anchor_city_id = str(anchor.get('city_id') or '') if anchor else ''
        scored: list[tuple[int, int, list[str], dict[str, Any]]] = []
        for candidate in self.grounding_candidates:
            if not self._passes_city_filter(candidate, city_id, city_hint):
                continue
            if anchor_city_id and candidate.get('city_id') != anchor_city_id:
                continue
            if anchor and candidate.get('candidate_id') == anchor.get('candidate_id'):
                continue
            (score, matched) = grounding_match_detail(candidate, blob_parts)
            (score, matched) = self._apply_field_queries(candidate, field_queries, score, matched)
            if has_query and score <= 0:
                continue
            distance = self._anchor_distance(anchor_loc, candidate)
            if anchor and near_radius_m and (distance is not None) and (distance > near_radius_m):
                continue
            if distance is not None:
                score += max(0, 5 - distance // 200)
            scored.append((score, distance if distance is not None else _FAR, matched, candidate))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if has_query and (not scored) and keyword and (not field_queries):
            scored = self._fuzzy_name_matches(keyword, city_id=city_id, city_hint=city_hint, anchor=anchor, anchor_loc=anchor_loc, anchor_city_id=anchor_city_id, near_radius_m=near_radius_m)
        candidates = []
        for (score, distance, matched, candidate) in scored[:limit]:
            item = public_grounding_candidate(candidate)
            anchor_distance = None if distance == _FAR else distance
            if anchor_distance is not None:
                item['distance_to_anchor_m'] = anchor_distance
            item['match'] = {'score': score, 'matched_fields': matched, 'method': 'fuzzy' if 'name_fuzzy' in matched else 'lexical', 'explanation': explain_match(matched, anchor_distance)}
            candidates.append(item)
        response: dict[str, Any] = {'candidates': candidates}
        return response

    @app_tool(OperationType.WRITE, summary='提交 grounding 选择')
    def select_grounded_location(self, args: dict[str, Any]) -> dict[str, Any]:
        candidate_id = str(args.get('candidate_id', ''))
        selected = self._candidate_by_id(candidate_id) or self._candidate_by_name(candidate_id)
        payload = public_grounding_candidate(selected) if selected else {}
        return {'selected': payload, 'identified_location': payload.get('location', {}) if payload else {}, 'rationale': args.get('rationale', '')}

    @app_tool(OperationType.READ, summary='搜索周边地点')
    def get_nearby(self, args: dict[str, Any]) -> dict[str, Any]:
        center = self._place_by_id(args.get('place_id', ''))
        radius = int(args.get('radius_m', 500) or 500)
        place_type = args.get('place_type', '')
        center_loc = center.get('location', {}) if center else {}
        nearby = []
        for place in self.places:
            if center and place.get('place_id') == center.get('place_id'):
                continue
            if place_type and place.get('primary_type') != place_type:
                continue
            distance = distance_m(center_loc, place.get('location', {})) if center_loc else 0
            if not center_loc or distance <= max(radius, 1000):
                item = place_payload(place)
                item['distance_m'] = round(distance)
                nearby.append(item)
        nearby.sort(key=lambda item: item.get('distance_m', 0))
        return {'nearby': nearby[:5]}

    def get_state(self) -> dict[str, Any]:
        return {'place_count': len(self.places), 'candidate_count': len(self.grounding_candidates), 'cities': sorted(self.city_names)}

    @staticmethod
    def _passes_city_filter(candidate: dict[str, Any], city_id: str, city_hint: str) -> bool:
        if city_id and candidate.get('city_id') != city_id:
            return False
        if city_hint and city_hint not in norm_query(f"{candidate.get('city_id', '')} {candidate.get('city_name', '')}"):
            return False
        return True

    def _place_by_id(self, place_id: str) -> dict[str, Any] | None:
        for place in self.places:
            if place.get('place_id') == place_id:
                return place
        return None
    _FIELD_QUERY_WEIGHTS = {'category': 2, 'district': 3, 'address': 3}
    _FUZZY_NAME_THRESHOLD = 0.78

    def _apply_field_queries(self, candidate: dict[str, Any], field_queries: list[tuple[str, str]], score: int, matched: list[str]) -> tuple[int, list[str]]:
        fields = candidate.get('_fields', {})
        for (field, term) in field_queries:
            if term and term in fields.get(field, ''):
                score += self._FIELD_QUERY_WEIGHTS.get(field, 2)
                if field not in matched:
                    matched.append(field)
        return (score, matched)

    def _anchor_distance(self, anchor_loc: dict[str, Any], candidate: dict[str, Any]) -> int | None:
        if not anchor_loc or not candidate.get('location'):
            return None
        return round(distance_m(anchor_loc, candidate['location']))

    def _fuzzy_name_matches(self, keyword: str, *, city_id: str, city_hint: str, anchor: dict[str, Any] | None, anchor_loc: dict[str, Any], anchor_city_id: str, near_radius_m: int) -> list[tuple[int, int, list[str], dict[str, Any]]]:
        matches: list[tuple[int, int, list[str], dict[str, Any]]] = []
        for candidate in self.grounding_candidates:
            if not self._passes_city_filter(candidate, city_id, city_hint):
                continue
            if anchor_city_id and candidate.get('city_id') != anchor_city_id:
                continue
            if anchor and candidate.get('candidate_id') == anchor.get('candidate_id'):
                continue
            names = [str(candidate.get('name') or ''), *candidate.get('_aliases', [])]
            similarity = max((bounded_name_similarity(keyword, name) for name in names), default=0.0)
            if similarity < self._FUZZY_NAME_THRESHOLD:
                continue
            distance = self._anchor_distance(anchor_loc, candidate)
            if anchor and near_radius_m and (distance is not None) and (distance > near_radius_m):
                continue
            score = round(similarity * 100)
            if distance is not None:
                score += max(0, 5 - distance // 200)
            matches.append((score, distance if distance is not None else _FAR, ['name_fuzzy'], candidate))
        matches.sort(key=lambda item: (-item[0], item[1]))
        return matches

    def _resolve_anchor(self, near: str, city_id: str, city_hint: str) -> dict[str, Any] | None:
        """Resolve a free-text anchor (e.g. '静安寺', '某商场') to a map location.

        Pure name/alias lookup over map metadata — the agent supplies the anchor
        phrase; MapApp only geolocates it, it does not infer it from images.
        """
        target = norm_query(near)
        if not target:
            return None
        for candidate in self.grounding_candidates:
            if not self._passes_city_filter(candidate, city_id, city_hint):
                continue
            if target in candidate.get('_fields', {}).get('name', ''):
                return candidate
        for candidate in self.grounding_candidates:
            if not self._passes_city_filter(candidate, city_id, city_hint):
                continue
            if target in candidate.get('_search_text', ''):
                return candidate
        return None

    def _candidate_by_id(self, candidate_id: str) -> dict[str, Any] | None:
        target = str(candidate_id or '')
        for candidate in self.grounding_candidates:
            if candidate.get('candidate_id') == target:
                return candidate
        return None

    def _candidate_by_name(self, name: str) -> dict[str, Any] | None:
        target = norm_query(name)
        for candidate in self.grounding_candidates:
            if target and target in candidate.get('_search_text', ''):
                return candidate
        return None

    def _build_grounding_candidates(self) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for place in self.places:
            item = grounding_candidate_from_place(place, self.city_names.get(place.get('city_id', ''), ''))
            if item['candidate_id'] not in seen:
                candidates.append(item)
                seen.add(item['candidate_id'])
        for character in self.characters:
            item = grounding_candidate_from_character(character)
            if item and item['candidate_id'] not in seen:
                candidates.append(item)
                seen.add(item['candidate_id'])
        return candidates
