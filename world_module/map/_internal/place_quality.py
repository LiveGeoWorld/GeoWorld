"""Place-level data quality: validation, consistency checks, de-duplication.

This module is the single source of truth for "is this place a good, usable
data point?". It is intentionally free of any pipeline-specific shape so it can
be reused by:

  * the generation pipeline (on enriched place *summaries*, before world build),
  * the offline ``clean_world`` tool (on built ``world.json`` *places*).

Both callers convert their objects into a normalized :class:`PlaceCandidate`,
run :func:`assess_places`, and act on the returned :class:`PlaceVerdict` map.

Checks performed
----------------
Hard failures (place is dropped):
  * ``missing_coordinates``        — no/zero lat&lng
  * ``outside_bbox``               — coordinate not inside the seed bbox
  * ``coordinate_at_city_center``  — coordinate snapped onto the city center
                                     (classic geocode-fallback garbage)
  * ``duplicate_near``             — same name within a short distance;
                                     collapsed to the best listing
  * ``duplicate_name_conflict``    — a junk twin (Plus-Code / no-address) sharing
                                     a name with a cleaner listing elsewhere.
                                     Legitimate chain branches are NOT dropped.

Soft flags (place is kept, but recorded for review):
  * ``plus_code_address``          — address is only an Open Location Code
  * ``missing_address``            — no address at all
  * ``far_outlier``                — very far from the city center but in bbox
  * ``address_coordinate_mismatch``— address admin-area (区/县) disagrees with
                                     the coordinate's reverse-geocoded area.
                                     Soft only: administrative renames and
                                     boundaries make this false-positive-prone.
  * ``city_mismatch``              — address city (市) disagrees with coordinate
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from math import asin, cos, radians, sin, sqrt
from typing import Any, Callable, Optional

# ── Tunables ──────────────────────────────────────────────────────────────
# Same-name places within this distance are treated as one place listed twice
# (collapsed). Kept small so legitimate chain branches stay separate.
DEFAULT_NAME_DUP_DISTANCE_M = 120.0
# Coordinate essentially identical to the city center == geocode-fallback junk.
DEFAULT_CITY_CENTER_EPS_M = 15.0
DEFAULT_FAR_OUTLIER_M = 9000.0

# Soft flags that, combined with a cleaner same-name sibling, mark a place as a
# junk twin (e.g. a Plus-Code "ghost" listing duplicating a real branch).
_SOFT_JUNK_FLAGS = {"plus_code_address", "missing_address", "far_outlier"}

# Flags that cause a place to be dropped. NOTE: address/coordinate admin
# mismatches are deliberately NOT here — administrative renames (e.g. Shanghai
# 闸北区 merged into 静安区 in 2015) and district-boundary cases make them too
# false-positive-prone to delete on. They are recorded as soft flags for review.
HARD_FLAGS = {
    "missing_coordinates",
    "outside_bbox",
    "coordinate_at_city_center",
    "duplicate_name_conflict",
    "duplicate_near",
}

# Open Location Code (Plus Code), e.g. "6FRV+VXX" or "WF8RFRV+VXX".
_PLUS_CODE_RE = re.compile(r"[23456789CFGHJMPQRVWX]{2,8}\+[23456789CFGHJMPQRVWX]{2,4}", re.IGNORECASE)
# CJK administrative tokens: 黄浦区 / 浦东新区 / 闵行区 / 嘉定县 / 渋谷区 …
_CN_DISTRICT_RE = re.compile(r"[\u4e00-\u9fa5\u3040-\u30ff]{1,8}?(?:新区|区|县)")
_CN_CITY_RE = re.compile(r"[\u4e00-\u9fa5]{1,8}?市")
_STREET_RE = re.compile(r"(?:\d+\s*号)|(?:[路街道巷弄])|(?:\d+\s*(?:st|street|ave|avenue|rd|road|blvd))", re.IGNORECASE)

# Address components returned by reverse geocoding that name an admin area.
_ADMIN_TYPES = {
    "administrative_area_level_1",
    "administrative_area_level_2",
    "administrative_area_level_3",
    "locality",
    "sublocality",
    "sublocality_level_1",
    "postal_town",
}


@dataclass
class PlaceCandidate:
    """Normalized view of a place used for quality assessment."""

    key: str
    name: str
    address: str
    lat: Optional[float]
    lng: Optional[float]
    rating_count: int = 0
    raw: Any = None


@dataclass
class PlaceVerdict:
    key: str
    keep: bool = True
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    duplicate_of: Optional[str] = None

    def add(self, flag: str, reason: str = "") -> None:
        if flag not in self.flags:
            self.flags.append(flag)
        if reason:
            self.reasons.append(reason)
        if flag in HARD_FLAGS:
            self.keep = False


# ── Small string / geo helpers ──────────────────────────────────────────────
def normalize_name(name: str) -> str:
    value = str(name or "").lower().strip()
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[\(\)（）·,，.。\-_/|]+", "", value)
    return value


def is_plus_code_address(address: str) -> bool:
    """True when the address is essentially only an Open Location Code.

    Plus-code-only addresses signal a Google listing with no real street
    address — a strong "low quality / ghost listing" indicator.
    """
    text = str(address or "").strip()
    if not text:
        return False
    match = _PLUS_CODE_RE.search(text)
    if not match:
        return False
    # If there is a real street token alongside the plus code, treat as real.
    return not _STREET_RE.search(text)


def has_real_street(address: str) -> bool:
    return bool(_STREET_RE.search(str(address or "")))


def cn_districts(text: str) -> set[str]:
    return {m.group(0) for m in _CN_DISTRICT_RE.finditer(str(text or ""))}


def cn_cities(text: str) -> set[str]:
    return {m.group(0) for m in _CN_CITY_RE.finditer(str(text or ""))}


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371000.0
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * radius * asin(sqrt(a))


def _has_coords(c: PlaceCandidate) -> bool:
    if c.lat is None or c.lng is None:
        return False
    return not (abs(float(c.lat)) < 1e-7 and abs(float(c.lng)) < 1e-7)


def _in_bbox(lat: float, lng: float, bbox: dict[str, float]) -> bool:
    return (
        float(bbox["south"]) <= lat <= float(bbox["north"])
        and float(bbox["west"]) <= lng <= float(bbox["east"])
    )


# ── Reverse-geocode adapter (optional, Google) ──────────────────────────────
def reverse_admin_via_maps_client(maps_client: Any, language: str = "zh-CN") -> Callable[[float, float], set[str]]:
    """Build a cached ``(lat, lng) -> {admin tokens}`` function from a maps client.

    Coordinates are rounded to ~110 m so nearby places share a single reverse
    geocode call. Failures degrade to an empty token set (no mismatch claimed).
    """
    cache: dict[tuple[float, float], set[str]] = {}

    def fn(lat: float, lng: float) -> set[str]:
        cache_key = (round(float(lat), 3), round(float(lng), 3))
        if cache_key in cache:
            return cache[cache_key]
        tokens: set[str] = set()
        try:
            result = maps_client.reverse_geocode(lat, lng, language=language)
            for comp in result.get("address_components", []) or []:
                if set(comp.get("types", [])) & _ADMIN_TYPES:
                    long_name = comp.get("long_name") or comp.get("short_name")
                    if long_name:
                        tokens.add(str(long_name))
            formatted = result.get("formatted_address", "")
            tokens |= cn_districts(formatted) | cn_cities(formatted)
        except Exception:
            tokens = set()
        cache[cache_key] = tokens
        return tokens

    return fn


# ── Scoring for "which duplicate to keep" ───────────────────────────────────
def _quality_score(candidate: PlaceCandidate, flags: list[str]) -> tuple:
    address_ok = 0 if (
        "plus_code_address" in flags
        or "missing_address" in flags
        or "address_coordinate_mismatch" in flags
        or "city_mismatch" in flags
    ) else 1
    street = 1 if has_real_street(candidate.address) else 0
    return (address_ok, street, int(candidate.rating_count or 0))


# ── Main entry point ─────────────────────────────────────────────────────────
def assess_places(
    candidates: list[PlaceCandidate],
    *,
    bbox: Optional[dict[str, float]] = None,
    city_center: Optional[dict[str, float]] = None,
    reverse_admin_fn: Optional[Callable[[float, float], set[str]]] = None,
    name_dup_distance_m: float = DEFAULT_NAME_DUP_DISTANCE_M,
    city_center_eps_m: float = DEFAULT_CITY_CENTER_EPS_M,
    far_outlier_m: float = DEFAULT_FAR_OUTLIER_M,
) -> dict[str, PlaceVerdict]:
    """Assess a list of candidates; return ``{key: PlaceVerdict}``.

    The function is deterministic and side-effect free. Hard-failed candidates
    are excluded from duplicate resolution so a junk twin can never win.
    """
    verdicts: dict[str, PlaceVerdict] = {c.key: PlaceVerdict(key=c.key) for c in candidates}

    center = None
    if city_center and city_center.get("lat") is not None and city_center.get("lng") is not None:
        center = (float(city_center["lat"]), float(city_center["lng"]))

    # ── Pass 1: per-place intrinsic + consistency checks ──
    for c in candidates:
        v = verdicts[c.key]
        if not _has_coords(c):
            v.add("missing_coordinates", "no usable lat/lng")
            continue
        lat, lng = float(c.lat), float(c.lng)

        if bbox and not _in_bbox(lat, lng, bbox):
            v.add("outside_bbox", "coordinate outside seed bbox")

        if center is not None:
            dist_center = haversine_m(lat, lng, center[0], center[1])
            if dist_center <= city_center_eps_m:
                v.add("coordinate_at_city_center", f"{dist_center:.0f}m from city center (geocode fallback)")
            elif dist_center > far_outlier_m:
                v.add("far_outlier", f"{dist_center / 1000:.1f}km from city center")

        if not str(c.address or "").strip():
            v.add("missing_address", "empty address")
        elif is_plus_code_address(c.address):
            v.add("plus_code_address", "address is only a Plus Code")

        # Reverse-geocode admin consistency (Google-only, optional).
        if reverse_admin_fn is not None and v.keep:
            rev_tokens = reverse_admin_fn(lat, lng)
            if rev_tokens:
                addr_d = cn_districts(c.address)
                rev_d = cn_districts(" ".join(rev_tokens))
                if addr_d and rev_d and addr_d.isdisjoint(rev_d):
                    v.add(
                        "address_coordinate_mismatch",
                        f"address says {sorted(addr_d)} but coordinate is in {sorted(rev_d)}",
                    )
                addr_c = cn_cities(c.address)
                rev_c = cn_cities(" ".join(rev_tokens))
                if addr_c and rev_c and addr_c.isdisjoint(rev_c):
                    v.add(
                        "city_mismatch",
                        f"address city {sorted(addr_c)} but coordinate city {sorted(rev_c)}",
                    )

    # ── Pass 2: name + proximity de-duplication (survivors only) ──
    #
    # Two failure modes to separate cleanly:
    #   * the SAME real place listed twice → collapse (duplicate_near),
    #   * a junk "ghost" listing sharing a name with a real place far away
    #     → drop the junk one (duplicate_name_conflict).
    # Legitimate chain branches (same name, far apart, all clean) are KEPT.
    survivors = [c for c in candidates if verdicts[c.key].keep and normalize_name(c.name)]
    groups: dict[str, list[PlaceCandidate]] = {}
    for c in survivors:
        groups.setdefault(normalize_name(c.name), []).append(c)

    def is_soft_junk(cand: PlaceCandidate) -> bool:
        return bool(set(verdicts[cand.key].flags) & _SOFT_JUNK_FLAGS)

    for members in groups.values():
        if len(members) < 2:
            continue

        # (a) Collapse near-duplicates inside each spatial cluster.
        clusters = _single_linkage_clusters(members, name_dup_distance_m)
        cluster_reps: list[PlaceCandidate] = []
        for cluster in clusters:
            best = max(cluster, key=lambda c: _quality_score(c, verdicts[c.key].flags))
            cluster_reps.append(best)
            for c in cluster:
                if c.key == best.key:
                    continue
                dist = haversine_m(float(c.lat), float(c.lng), float(best.lat), float(best.lng))
                verdicts[c.key].duplicate_of = best.key
                verdicts[c.key].add("duplicate_near", f"same place as kept '{best.name}' ({dist:.0f}m away)")

        # (b) Among far-apart representatives, only drop junk twins when a
        # clean same-name sibling exists. Otherwise keep them all (chains).
        if len(cluster_reps) > 1:
            clean_reps = [r for r in cluster_reps if not is_soft_junk(r)]
            if clean_reps:
                anchor = max(clean_reps, key=lambda c: _quality_score(c, verdicts[c.key].flags))
                for r in cluster_reps:
                    if r.key != anchor.key and is_soft_junk(r):
                        dist = haversine_m(float(r.lat), float(r.lng), float(anchor.lat), float(anchor.lng))
                        verdicts[r.key].duplicate_of = anchor.key
                        verdicts[r.key].add(
                            "duplicate_name_conflict",
                            f"junk twin of cleaner '{anchor.name}' ({dist:.0f}m away)",
                        )

    return verdicts


def _single_linkage_clusters(members: list[PlaceCandidate], threshold_m: float) -> list[list[PlaceCandidate]]:
    """Group members so any two within ``threshold_m`` land in the same cluster."""
    parent = {c.key: c.key for c in members}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            a, b = members[i], members[j]
            if haversine_m(float(a.lat), float(a.lng), float(b.lat), float(b.lng)) <= threshold_m:
                union(a.key, b.key)

    clusters: dict[str, list[PlaceCandidate]] = {}
    for c in members:
        clusters.setdefault(find(c.key), []).append(c)
    return list(clusters.values())


# ── Adapters from concrete shapes ───────────────────────────────────────────
def candidate_from_summary(summary: dict[str, Any]) -> PlaceCandidate:
    """From an enriched place summary (google/osm enricher output)."""
    loc = summary.get("location") or {}
    key = (
        summary.get("source_place_id")
        or summary.get("google_place_id")
        or summary.get("osm_id")
        or summary.get("name", "")
    )
    return PlaceCandidate(
        key=str(key),
        name=str(summary.get("name", "")),
        address=str(summary.get("address", "")),
        lat=loc.get("lat"),
        lng=loc.get("lng"),
        rating_count=int(summary.get("user_rating_count") or 0),
        raw=summary,
    )


def candidate_from_world_place(place: dict[str, Any]) -> PlaceCandidate:
    """From a built ``world.json`` place object."""
    loc = place.get("location") or {}
    projection = place.get("map_projection") or {}
    return PlaceCandidate(
        key=str(place.get("place_id", "")),
        name=str(place.get("name", "")),
        address=str(place.get("address", "")),
        lat=loc.get("lat"),
        lng=loc.get("lng"),
        rating_count=int(projection.get("user_rating_count") or 0),
        raw=place,
    )


def summarize_verdicts(verdicts: dict[str, PlaceVerdict]) -> dict[str, Any]:
    """Aggregate counts for reports."""
    dropped = [v for v in verdicts.values() if not v.keep]
    kept = [v for v in verdicts.values() if v.keep]
    flag_counts: dict[str, int] = {}
    for v in verdicts.values():
        for flag in v.flags:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1
    return {
        "total": len(verdicts),
        "kept": len(kept),
        "dropped": len(dropped),
        "flag_counts": flag_counts,
        "dropped_detail": [
            {
                "key": v.key,
                "flags": v.flags,
                "reasons": v.reasons,
                "duplicate_of": v.duplicate_of,
            }
            for v in dropped
        ],
        "kept_with_flags": [
            {"key": v.key, "flags": v.flags, "reasons": v.reasons}
            for v in kept
            if v.flags
        ],
    }
