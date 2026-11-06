"""Google Maps API real client — Nearby Search, Place Details, Street View URL."""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from shared.config_loader import PipelineConfig
from shared.external_usage import record_external_usage

_NEARBY_SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"
_PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
_STREET_VIEW_URL = "https://maps.googleapis.com/maps/api/streetview"
_STREET_VIEW_METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
_STREET_VIEW_TIMEOUT_SECONDS = 120

# Field masks by SKU tier
FIELD_MASK_PRO = (
    "places.id,places.displayName,places.formattedAddress,"
    "places.location,places.types,places.primaryType,places.businessStatus"
)
FIELD_MASK_ENTERPRISE = (
    FIELD_MASK_PRO + ","
    "places.rating,places.userRatingCount,places.regularOpeningHours,"
    "places.currentOpeningHours,places.nationalPhoneNumber,places.websiteUri,"
    "places.priceLevel,places.priceRange,places.reservable,places.dineIn,"
    "places.takeout,places.delivery,places.curbsidePickup"
)


class GoogleMapsClient:
    """Thin wrapper around Google Maps Platform APIs (Places API New + Street View Static)."""

    def __init__(self, config: Optional[PipelineConfig] = None):
        self.config = config or PipelineConfig()
        self._api_profiles: list[tuple[str, str]] = []
        self._api_keys: list[str] = []

        # Collect configured profiles, then rotate from the selected current
        # profile.  Fallback remains enabled for quota/reliability, but the
        # rotation order is explicit and auditable.
        raw = self.config._raw
        if "google_maps" in raw:
            self._api_profiles.append(("google_maps", raw["google_maps"]["api_key"]))
        for i in range(2, 10):
            k = f"google_maps_{i}"
            if k in raw and raw[k].get("api_key"):
                self._api_profiles.append((k, raw[k]["api_key"]))

        requested_start = str(raw.get("google_maps_start_key") or "google_maps")
        profile_names = [name for name, _key in self._api_profiles]
        if requested_start not in profile_names:
            raise KeyError(
                f"Google Maps start profile {requested_start!r} is not configured. "
                f"Available profiles: {profile_names}"
            )
        start_index = profile_names.index(requested_start)
        self._api_profiles = self._api_profiles[start_index:] + self._api_profiles[:start_index]
        self._api_keys = [key for _name, key in self._api_profiles]
        self._api_key_profile = self._api_profiles[0][0] if self._api_profiles else ""

        self._api_key = self._api_keys[0] if self._api_keys else ""
        self._session = requests.Session()
        self._telemetry: dict[str, Any] = {
            "service": "google_maps", "start_profile": self._api_key_profile,
            "configured_profile_order": [name for name, _key in self._api_profiles],
            "requests_total": 0, "requests_by_profile": {}, "api_retries": 0, "api_failures": 0,
            "api_elapsed_s": 0.0, "requests_by_endpoint": {}, "failures_by_endpoint": {},
            "cost_status": "pricing_not_configured", "money_estimate": None,
        }

    def telemetry_snapshot(self) -> dict[str, Any]:
        """Measured API facts; billing is deliberately unknown without a price table."""
        snapshot = dict(self._telemetry)
        snapshot["requests_by_endpoint"] = dict(self._telemetry["requests_by_endpoint"])
        snapshot["requests_by_profile"] = dict(self._telemetry["requests_by_profile"])
        snapshot["failures_by_endpoint"] = dict(self._telemetry["failures_by_endpoint"])
        snapshot["api_elapsed_s"] = round(float(snapshot["api_elapsed_s"]), 3)
        return snapshot

    def _request_with_api_keys(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: int = 15,
        key_location: str = "header",
    ) -> Dict[str, Any]:
        """Request JSON, trying configured Google API keys in order.

        Some local configs keep a disabled key first and a working fallback key
        second. Generation should not silently fall back to node-only geometry
        just because the first key is rejected.
        """
        profiles = self._api_profiles or [(self._api_key_profile or "unconfigured", self._api_key)]
        endpoint = url.split("?", 1)[0].rsplit("/", 1)[-1] or "request"
        if "searchNearby" in url: endpoint = "places_nearby"
        elif "searchText" in url: endpoint = "places_text"
        elif "directions" in url: endpoint = "routes"
        elif "geocode" in url: endpoint = "geocode"
        elif "/places/" in url: endpoint = "place_details"
        last_error: Exception | None = None
        for index, (profile_name, key) in enumerate(profiles):
            req_headers = dict(headers or {})
            req_params = dict(params or {})
            if key_location == "header":
                req_headers["X-Goog-Api-Key"] = key
            else:
                req_params["key"] = key
            try:
                started_at = time.monotonic()
                self._telemetry["requests_total"] += 1
                self._telemetry["requests_by_profile"][profile_name] = self._telemetry["requests_by_profile"].get(profile_name, 0) + 1
                self._telemetry["requests_by_endpoint"][endpoint] = self._telemetry["requests_by_endpoint"].get(endpoint, 0) + 1
                resp = self._session.request(
                    method,
                    url,
                    headers=req_headers,
                    params=req_params or None,
                    json=json_body,
                    timeout=timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("status") == "REQUEST_DENIED" and index < len(profiles) - 1:
                    record_external_usage(
                        provider="google_maps", operation=endpoint, status="provider_rejected",
                        elapsed_s=time.monotonic() - started_at,
                        usage={"request_units": 1}, profile=profile_name,
                    )
                    self._telemetry["api_retries"] += 1
                    last_error = RuntimeError(
                        f"Google API key rejected: {data.get('error_message', '')}"
                    )
                    continue
                record_external_usage(
                    provider="google_maps", operation=endpoint, status="success",
                    elapsed_s=time.monotonic() - started_at,
                    usage={"request_units": 1}, profile=profile_name,
                )
                return data
            except requests.HTTPError as exc:
                self._telemetry["api_failures"] += 1
                self._telemetry["failures_by_endpoint"][endpoint] = self._telemetry["failures_by_endpoint"].get(endpoint, 0) + 1
                status = exc.response.status_code if exc.response is not None else None
                record_external_usage(
                    provider="google_maps", operation=endpoint, status="failed",
                    elapsed_s=time.monotonic() - started_at,
                    usage={"request_units": 1}, profile=profile_name,
                    http_status=status, error_class=type(exc).__name__,
                )
                if status in {401, 403, 429} and index < len(profiles) - 1:
                    self._telemetry["api_retries"] += 1
                    last_error = exc
                    continue
                raise
            except Exception as exc:
                self._telemetry["api_failures"] += 1
                self._telemetry["failures_by_endpoint"][endpoint] = self._telemetry["failures_by_endpoint"].get(endpoint, 0) + 1
                record_external_usage(
                    provider="google_maps", operation=endpoint, status="failed",
                    elapsed_s=time.monotonic() - started_at,
                    usage={"request_units": 1}, profile=profile_name,
                    error_class=type(exc).__name__,
                )
                raise
            finally:
                self._telemetry["api_elapsed_s"] += time.monotonic() - started_at
        if last_error:
            raise last_error
        return {}

    # ── Nearby Search (New) ──
    def nearby_search(self, lat: float, lng: float, radius_m: int = 500,
                      included_types: Optional[List[str]] = None,
                      max_results: int = 10,
                      language: str = "zh-CN",
                      field_mask: Optional[str] = None) -> List[Dict[str, Any]]:
        headers = {
            "Content-Type": "application/json",
            "X-Goog-FieldMask": field_mask or FIELD_MASK_ENTERPRISE,
        }
        body: Dict[str, Any] = {
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": float(radius_m),
                }
            },
            "maxResultCount": max_results,
            "languageCode": language,
        }
        if included_types:
            body["includedTypes"] = included_types

        data = self._request_with_api_keys(
            "POST", _NEARBY_SEARCH_URL, json_body=body, headers=headers, timeout=15
        )
        return data.get("places", [])

    # ── Place Details (New) ──
    def place_details(self, place_id: str,
                      fields: Optional[List[str]] = None,
                      language: str = "zh-CN") -> Dict[str, Any]:
        if fields is None:
            fields = [
                "id", "displayName", "formattedAddress", "location",
                "types", "rating", "userRatingCount",
                "currentOpeningHours", "regularOpeningHours",
                "primaryType", "websiteUri", "nationalPhoneNumber",
                "priceLevel", "priceRange", "reservable", "dineIn", "takeout",
                "delivery", "curbsidePickup", "reviews",
            ]
        headers = {
            "Content-Type": "application/json",
            "X-Goog-FieldMask": ",".join(fields),
        }
        url = _PLACE_DETAILS_URL.format(place_id=place_id)
        return self._request_with_api_keys(
            "GET", url, headers=headers, params={"languageCode": language}, timeout=15
        )

    # ── Text Search (New) ──
    def text_search(self, query: str,
                    lat: Optional[float] = None,
                    lng: Optional[float] = None,
                    radius_m: Optional[int] = None,
                    included_type: Optional[str] = None,
                    max_results: int = 10,
                    language: str = "zh-CN",
                    field_mask: Optional[str] = None) -> List[Dict[str, Any]]:
        """Search places by text, optionally biased around a city center."""
        headers = {
            "Content-Type": "application/json",
            "X-Goog-FieldMask": field_mask or FIELD_MASK_ENTERPRISE,
        }
        body: Dict[str, Any] = {
            "textQuery": query,
            "maxResultCount": max_results,
            "languageCode": language,
        }
        if included_type:
            body["includedType"] = included_type
        if lat is not None and lng is not None and radius_m is not None:
            body["locationBias"] = {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": float(radius_m),
                }
            }
        data = self._request_with_api_keys(
            "POST", _TEXT_SEARCH_URL, json_body=body, headers=headers, timeout=15
        )
        return data.get("places", [])

    # ── Street View Static API ──
    def street_view_url(self, lat: float, lng: float,
                        heading: int = 0, pitch: int = 0,
                        fov: int = 90, size: str = "640x640") -> str:
        params = {
            "location": f"{lat},{lng}",
            "heading": heading,
            "pitch": pitch,
            "fov": fov,
            "size": size,
            "key": self._api_key,
        }
        return f"{_STREET_VIEW_URL}?{urlencode(params)}"

    def street_view_metadata(self, lat: float, lng: float, radius_m: int = 60) -> Dict[str, Any]:
        """Return Street View metadata using the configured key rotation."""
        return self._request_with_api_keys(
            "GET",
            _STREET_VIEW_METADATA_URL,
            params={"location": f"{lat},{lng}", "radius": str(radius_m)},
            timeout=_STREET_VIEW_TIMEOUT_SECONDS,
            key_location="params",
        )

    def download_street_view(self, lat: float, lng: float,
                             heading: int = 0, pitch: int = 0,
                             fov: int = 90, size: str = "640x640") -> bytes:
        params = {
            "location": f"{lat},{lng}", "heading": heading, "pitch": pitch,
            "fov": fov, "size": size,
        }
        last_error: Exception | None = None
        for index, (profile_name, key) in enumerate(self._api_profiles):
            try:
                started_at = time.monotonic()
                self._telemetry["requests_total"] += 1
                self._telemetry["requests_by_profile"][profile_name] = self._telemetry["requests_by_profile"].get(profile_name, 0) + 1
                self._telemetry["requests_by_endpoint"]["streetview_image"] = self._telemetry["requests_by_endpoint"].get("streetview_image", 0) + 1
                response = self._session.get(
                    _STREET_VIEW_URL,
                    params={**params, "key": key},
                    timeout=_STREET_VIEW_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                record_external_usage(
                    provider="google_maps", operation="streetview_image", status="success",
                    elapsed_s=time.monotonic() - started_at,
                    usage={"request_units": 1, "image_units": 1}, profile=profile_name,
                )
                return response.content
            except requests.HTTPError as exc:
                self._telemetry["api_failures"] += 1
                self._telemetry["failures_by_endpoint"]["streetview_image"] = self._telemetry["failures_by_endpoint"].get("streetview_image", 0) + 1
                last_error = exc
                status = exc.response.status_code if exc.response is not None else None
                record_external_usage(
                    provider="google_maps", operation="streetview_image", status="failed",
                    elapsed_s=time.monotonic() - started_at,
                    usage={"request_units": 1, "image_units": 1}, profile=profile_name,
                    http_status=status, error_class=type(exc).__name__,
                )
                if status in {401, 403, 429} and index < len(self._api_profiles) - 1:
                    self._telemetry["api_retries"] += 1
                    continue
                raise
            finally:
                self._telemetry["api_elapsed_s"] += time.monotonic() - started_at
        if last_error is not None:
            raise last_error
        raise RuntimeError("No Google Maps credential profiles are configured.")

    # ── Reverse Geocoding ──
    def reverse_geocode(self, lat: float, lng: float,
                        language: str = "zh-CN") -> Dict[str, Any]:
        """Reverse geocode coordinates to a structured address.

        Returns the first (most specific) result from Google Geocoding API.
        """
        params = {
            "latlng": f"{lat},{lng}",
            "language": language,
        }
        data = self._request_with_api_keys(
            "GET", _GEOCODE_URL, params=params, timeout=15, key_location="params"
        )
        # ZERO_RESULTS is a valid response for an individual coordinate.  It
        # must not be promoted to a provider outage by `geo_provider=auto`.
        if data.get("status") == "ZERO_RESULTS":
            return {"formatted_address": "", "address_components": [], "place_id": "", "types": []}
        if data.get("status") not in {None, "OK"}:
            raise RuntimeError(f"Google Geocoding API error {data.get('status')}: {data.get('error_message', '')}")
        results = data.get("results", [])
        if not results:
            return {"formatted_address": "", "address_components": [], "place_id": ""}
        first = results[0]
        return {
            "formatted_address": first.get("formatted_address", ""),
            "address_components": first.get("address_components", []),
            "place_id": first.get("place_id", ""),
            "types": first.get("types", []),
        }

    def geocode(self, address: str, language: str = "zh-CN",
                bounds: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """Forward geocode a city, district, road, or station name.

        bounds may contain north/south/east/west to bias the lookup.
        """
        params: Dict[str, Any] = {
            "address": address,
            "language": language,
        }
        if bounds:
            params["bounds"] = (
                f"{bounds['south']},{bounds['west']}|"
                f"{bounds['north']},{bounds['east']}"
            )
        data = self._request_with_api_keys(
            "GET", _GEOCODE_URL, params=params, timeout=15, key_location="params"
        )
        # See reverse_geocode: an unmatched individual name is not evidence
        # that Google Places/Routes is unavailable for the whole city build.
        if data.get("status") == "ZERO_RESULTS":
            return {
                "formatted_address": "",
                "address_components": [],
                "place_id": "",
                "types": [],
                "location": {},
                "viewport": {},
            }
        if data.get("status") not in {None, "OK"}:
            raise RuntimeError(f"Google Geocoding API error {data.get('status')}: {data.get('error_message', '')}")
        results = data.get("results", [])
        if not results:
            return {
                "formatted_address": "",
                "address_components": [],
                "place_id": "",
                "types": [],
                "location": {},
                "viewport": {},
            }
        first = results[0]
        geometry = first.get("geometry", {})
        location = geometry.get("location", {})
        return {
            "formatted_address": first.get("formatted_address", ""),
            "address_components": first.get("address_components", []),
            "place_id": first.get("place_id", ""),
            "types": first.get("types", []),
            "location": {"latitude": location.get("lat"), "longitude": location.get("lng")},
            "viewport": geometry.get("viewport", {}),
        }

    # ── Utility: compute heading from seed to POI ──
    @staticmethod
    def compute_heading(from_lat: float, from_lng: float,
                        to_lat: float, to_lng: float) -> int:
        lat1, lng1 = math.radians(from_lat), math.radians(from_lng)
        lat2, lng2 = math.radians(to_lat), math.radians(to_lng)
        d_lng = lng2 - lng1
        x = math.sin(d_lng) * math.cos(lat2)
        y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lng)
        heading = math.degrees(math.atan2(x, y))
        return int((heading + 360) % 360)

    # ── Routes API (Compute Routes) ──
    def compute_route(self, origin_lat: float, origin_lng: float,
                      dest_lat: float, dest_lng: float,
                      travel_mode: str = "DRIVE",
                      language: str = "en",
                      departure_time: str | None = None) -> Dict[str, Any]:
        """Compute a route between two points via Routes API.

        Args:
            travel_mode: DRIVE, WALK, BICYCLE, or TRANSIT
        Returns:
            {distance_m, duration_s, summary, steps: [...],
             polyline_points: [{lat, lng}, ...], encoded_polyline: str}
        """
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._api_keys[0],
            "X-Goog-FieldMask": (
                "routes.distanceMeters,routes.staticDuration,routes.description,"
                "routes.polyline.encodedPolyline,"
                "routes.legs.steps.staticDuration,routes.legs.steps.distanceMeters,"
                "routes.legs.steps.startLocation,routes.legs.steps.endLocation,"
                "routes.legs.steps.polyline,routes.legs.steps.travelMode,"
                "routes.legs.steps.navigationInstruction,"
                "routes.legs.steps.transitDetails"
            ),
        }
        body = {
            "origin": {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lng}}},
            "destination": {"location": {"latLng": {"latitude": dest_lat, "longitude": dest_lng}}},
            "travelMode": travel_mode,
            "languageCode": language,
            "polylineEncoding": "ENCODED_POLYLINE",
        }
        # Transit results are time-dependent. Builders should pass a frozen
        # task/world snapshot; the fallback keeps legacy callers reproducible.
        if travel_mode == "TRANSIT":
            body["departureTime"] = departure_time or "2026-01-05T09:00:00Z"
            body["computeAlternativeRoutes"] = True
            body["transitPreferences"] = {
                "allowedTravelModes": ["BUS", "SUBWAY", "TRAIN", "LIGHT_RAIL", "RAIL"],
                "routingPreference": "FEWER_TRANSFERS",
            }
        last_error = None
        data = None
        for index, (profile_name, key) in enumerate(self._api_profiles):
            headers["X-Goog-Api-Key"] = key
            try:
                started_at = time.monotonic()
                self._telemetry["requests_total"] += 1
                self._telemetry["requests_by_profile"][profile_name] = self._telemetry["requests_by_profile"].get(profile_name, 0) + 1
                self._telemetry["requests_by_endpoint"]["routes"] = self._telemetry["requests_by_endpoint"].get("routes", 0) + 1
                resp = self._session.post(_ROUTES_URL, json=body, headers=headers, timeout=20)
                resp.raise_for_status()
                data = resp.json()
                last_error = None
                record_external_usage(
                    provider="google_maps", operation="routes", status="success",
                    elapsed_s=time.monotonic() - started_at,
                    usage={"request_units": 1}, profile=profile_name,
                )
                break
            except Exception as exc:
                self._telemetry["api_failures"] += 1
                self._telemetry["failures_by_endpoint"]["routes"] = self._telemetry["failures_by_endpoint"].get("routes", 0) + 1
                last_error = exc
                record_external_usage(
                    provider="google_maps", operation="routes", status="failed",
                    elapsed_s=time.monotonic() - started_at,
                    usage={"request_units": 1}, profile=profile_name,
                    error_class=type(exc).__name__,
                )
                if index < len(self._api_profiles) - 1:
                    self._telemetry["api_retries"] += 1
                    continue
            finally:
                self._telemetry["api_elapsed_s"] += time.monotonic() - started_at

        if last_error is not None:
            raise last_error

        routes = data.get("routes", [])
        if not routes:
            return None  # No route exists for this mode → caller marks available=False

        route = routes[0]
        # staticDuration is like "1800s"
        dur_str = route.get("staticDuration", "0s")
        duration_s = int(dur_str.rstrip("s")) if dur_str else 0
        encoded = (route.get("polyline") or {}).get("encodedPolyline", "")
        polyline_points = decode_polyline(encoded) if encoded else []

        return {
            "distance_m": route.get("distanceMeters", 0),
            "duration_s": duration_s,
            "summary": route.get("description", ""),
            "steps": self._extract_steps(route),
            "polyline_points": polyline_points,
            "encoded_polyline": encoded,
        }

    @staticmethod
    def _extract_steps(route: Dict) -> List[Dict]:
        steps = []
        for leg in route.get("legs", []):
            for step in leg.get("steps", []):
                dur_str = step.get("staticDuration", "0s")
                s = {
                    "distance_m": step.get("distanceMeters", 0),
                    "duration_s": int(dur_str.rstrip("s")) if dur_str else 0,
                }
                for key in ("startLocation", "endLocation", "polyline", "travelMode"):
                    if key in step:
                        s[key] = step[key]
                nav = step.get("navigationInstruction")
                if nav:
                    s["instruction"] = nav.get("instructions", "")
                td = step.get("transitDetails")
                if td:
                    s["transit_details"] = td
                steps.append(s)
        return steps

    @staticmethod
    def haversine_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        R = 6371000
        dlat = math.radians(lat2 - lat1)
        dlng = math.radians(lng2 - lng1)
        a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def decode_polyline(encoded: str) -> List[Dict[str, float]]:
    """Decode a Google Encoded Polyline Algorithm Format string."""
    if not encoded:
        return []
    points: List[Dict[str, float]] = []
    index = 0
    lat = 0
    lng = 0
    length = len(encoded)
    while index < length:
        result = 0
        shift = 0
        while True:
            if index >= length:
                return points
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        delta_lat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += delta_lat

        result = 0
        shift = 0
        while True:
            if index >= length:
                return points
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        delta_lng = ~(result >> 1) if (result & 1) else (result >> 1)
        lng += delta_lng

        points.append({"lat": lat * 1e-5, "lng": lng * 1e-5})
    return points
