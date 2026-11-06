"""Version-bound website-backed service detail snapshots.

This module enriches a *complete frozen map world* before task construction.
It deliberately keeps merchant content out of the map artifact: MapApp remains
the authority for discovery and map facts, while LocalServiceApp receives a
separate, identity-aligned projection of source-backed service information.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from shared.io import read_json, write_json


SERVICE_DETAIL_CACHE_SCHEMA = "geoword.service_detail_cache.v1"
LOCAL_SERVICE_WORLD_STATE_SCHEMA = "geoword.local_service_world_state.v1"
SERVICE_DETAIL_CHECKPOINT_SCHEMA = "geoword.service_detail_checkpoint.v1"
_USER_AGENT = "GeoAgentBench-ServiceDetailSnapshot/1.0 (+research benchmark)"
_MAX_LINKED_DOCUMENTS = 3
_DETAIL_LINK_TERMS = (
    "menu", "food", "dining", "eat", "about", "our-story", "our_story",
    "service", "services", "product", "products", "shop", "offer",
    "opening", "hours", "contact", "visit", "locations",
)


class _VisibleTextParser(HTMLParser):
    """Extract visible HTML text without treating markup as a merchant fact."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self._parts.append(data)

    def visible_text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._parts)).strip()


class _MerchantLinkParser(HTMLParser):
    """Capture anchors so only selected same-site pages are fetched."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._label_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._href = href
            self._label_parts = []

    def handle_data(self, data: str) -> None:
        if self._href and data.strip():
            self._label_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self._links.append((self._href, " ".join(self._label_parts)))
            self._href = None
            self._label_parts = []

    def links(self) -> list[tuple[str, str]]:
        return list(self._links)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _location(location: Any) -> dict[str, float] | None:
    if not isinstance(location, dict):
        return None
    try:
        return {"lat": float(location["lat"]), "lng": float(location["lng"])}
    except (KeyError, TypeError, ValueError):
        return None


def public_identity(place: dict[str, Any]) -> dict[str, Any] | None:
    name = str(place.get("name") or "").strip()
    location = _location(place.get("location"))
    if not name or location is None:
        return None
    return {"name": name, "location": location}


def identity_key(identity: dict[str, Any]) -> str:
    location = identity.get("location") if isinstance(identity.get("location"), dict) else {}
    return "|".join((
        str(identity.get("name") or "").strip().casefold(),
        f"{float(location.get('lat')):.6f}",
        f"{float(location.get('lng')):.6f}",
    ))


def place_identity_signature(places: list[dict[str, Any]]) -> str:
    identities = [public_identity(place) for place in places if isinstance(place, dict)]
    canonical = [identity for identity in identities if identity is not None]
    canonical.sort(key=identity_key)
    payload = json.dumps(canonical, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def website_uri(place: dict[str, Any]) -> str:
    projection = place.get("map_projection") or {}
    details = projection.get("google_place_details") or {}
    value = details.get("website_uri") or projection.get("website_uri") or ""
    return str(value).strip()


def _checkpoint_binding(world: dict[str, Any], map_world_sha256: str) -> dict[str, Any]:
    places = [place for place in world.get("places") or [] if isinstance(place, dict)]
    city = (world.get("cities") or [{}])[0] if isinstance(world.get("cities"), list) else {}
    return {
        "city_id": str(city.get("city_id") or ""),
        "map_world_sha256": map_world_sha256,
        "source_place_count": len(places),
        "source_place_identity_signature": place_identity_signature(places),
    }


def read_service_detail_checkpoint(
    checkpoint_path: str | Path,
    world: dict[str, Any],
    *,
    map_world_sha256: str,
) -> dict[str, Any]:
    """Read only complete checkpoint events bound to the current frozen map."""
    path = Path(checkpoint_path)
    empty = {"valid": False, "records": [], "website_requests": 0}
    if not path.is_file():
        return empty
    expected_binding = _checkpoint_binding(world, map_world_sha256)
    records_by_identity: dict[str, dict[str, Any]] = {}
    website_requests = 0
    header_seen = False
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            kind = str(event.get("kind") or "")
            if kind == "header":
                if (event.get("schema_version") != SERVICE_DETAIL_CHECKPOINT_SCHEMA
                        or dict(event.get("world_binding") or {}) != expected_binding):
                    return empty
                header_seen = True
            elif header_seen and kind == "request":
                website_requests += 1
            elif header_seen and kind == "record" and isinstance(event.get("record"), dict):
                record = dict(event["record"])
                identity = public_identity(record)
                if identity is not None:
                    records_by_identity[identity_key(identity)] = record
    return {
        "valid": header_seen,
        "records": list(records_by_identity.values()),
        "website_requests": website_requests,
    }


def _prepare_service_detail_checkpoint(
    checkpoint_path: Path,
    world: dict[str, Any],
    *,
    map_world_sha256: str,
    resume: bool,
) -> dict[str, Any]:
    checkpoint = read_service_detail_checkpoint(
        checkpoint_path, world, map_world_sha256=map_world_sha256,
    ) if resume else {"valid": False, "records": [], "website_requests": 0}
    if checkpoint.get("valid"):
        return checkpoint
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "schema_version": SERVICE_DETAIL_CHECKPOINT_SCHEMA,
        "kind": "header",
        "world_binding": _checkpoint_binding(world, map_world_sha256),
    }
    checkpoint_path.write_text(
        json.dumps(header, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return {"valid": True, "records": [], "website_requests": 0}


def _append_checkpoint_event(checkpoint_path: Path, event: dict[str, Any]) -> None:
    with checkpoint_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        handle.write("\n")


def extract_visible_text(content: str, content_type: str) -> tuple[str, str]:
    """Return extracted text and status without claiming non-text is readable."""
    normalized_type = str(content_type or "").casefold()
    if "html" not in normalized_type:
        return "", "non_text_source"
    parser = _VisibleTextParser()
    parser.feed(content)
    parser.close()
    text = parser.visible_text()
    return text, "source_backed" if text else "empty_source_text"


def _canonical_url(uri: str) -> str:
    parts = urlsplit(str(uri or "").strip())
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path or "/", parts.query, ""))


def _merchant_detail_links(content: str, *, page_url: str, limit: int = _MAX_LINKED_DOCUMENTS) -> list[str]:
    """Return bounded same-host links whose label or URL suggests merchant detail."""
    base = urlsplit(page_url)
    if base.scheme not in {"http", "https"} or not base.netloc:
        return []
    parser = _MerchantLinkParser()
    parser.feed(content)
    parser.close()
    candidates: list[str] = []
    seen = {_canonical_url(page_url)}
    for href, label in parser.links():
        resolved = _canonical_url(urljoin(page_url, href))
        parsed = urlsplit(resolved)
        searchable = f"{label} {parsed.path} {parsed.query}".casefold()
        if (parsed.scheme not in {"http", "https"} or parsed.netloc != base.netloc
                or not any(term in searchable for term in _DETAIL_LINK_TERMS)
                or resolved in seen):
            continue
        candidates.append(resolved)
        seen.add(resolved)
        if len(candidates) >= limit:
            break
    return candidates


def fetch_website(uri: str, *, timeout_seconds: float = 20.0, max_bytes: int = 2_000_000) -> dict[str, Any]:
    """Fetch one already-recorded merchant URL with bounded response size."""
    request = Request(uri, headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.8,*/*;q=0.1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310 - URL comes from frozen map data
            raw = response.read(max_bytes + 1)
            final_url = str(response.geturl() or uri)
            content_type = str(response.headers.get_content_type() or "")
            charset = response.headers.get_content_charset() or "utf-8"
    except HTTPError as exc:
        return {"status": "http_error", "status_code": int(exc.code), "error": str(exc), "source_url": uri}
    except (URLError, OSError, ValueError, HTTPException) as exc:
        return {"status": "fetch_error", "error": str(exc), "source_url": uri}

    if len(raw) > max_bytes:
        return {
            "status": "response_too_large", "source_url": uri, "final_url": final_url,
            "content_type": content_type, "bytes": len(raw),
        }
    try:
        body = raw.decode(charset, errors="replace")
    except LookupError:
        body = raw.decode("utf-8", errors="replace")
    return {
        "status": "fetched", "source_url": uri, "final_url": final_url,
        "content_type": content_type, "bytes": len(raw), "body": body,
    }


def _document_from_fetch(uri: str, result: dict[str, Any], *, collected_at: str) -> dict[str, Any]:
    """Represent one retrieval, including non-text and failed-source states."""
    status = str(result.get("status") or "fetch_error")
    document: dict[str, Any] = {
        "source_url": uri,
        "final_url": str(result.get("final_url") or uri),
        "retrieved_at": collected_at,
        "retrieval_status": status,
        "content_type": str(result.get("content_type") or ""),
        "bytes": int(result.get("bytes") or 0),
        "content_hash": "",
        "visible_text": "",
    }
    if status != "fetched":
        if result.get("error"):
            document["error"] = str(result["error"])
        document["extraction_status"] = status
        return document
    source_text, extraction_status = extract_visible_text(
        str(result.get("body") or ""), str(result.get("content_type") or ""),
    )
    document["extraction_status"] = extraction_status
    document["visible_text"] = source_text
    document["content_hash"] = hashlib.sha256(source_text.encode("utf-8")).hexdigest() if source_text else ""
    return document


def _record_from_fetch(
    identity: dict[str, Any], uri: str, result: dict[str, Any], *,
    collected_at: str, fetcher: Callable[[str], dict[str, Any]],
    request_callback: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], int]:
    """Freeze homepage plus bounded, same-host merchant detail documents."""
    record: dict[str, Any] = {**identity, "website_uri": uri, "collected_at": collected_at}
    documents = [_document_from_fetch(uri, result, collected_at=collected_at)]
    request_count = 1
    if result.get("status") == "fetched" and "html" in str(result.get("content_type") or "").casefold():
        root_url = str(result.get("final_url") or uri)
        for link in _merchant_detail_links(str(result.get("body") or ""), page_url=root_url):
            if request_callback is not None:
                request_callback(link)
            linked_result = fetcher(link)
            documents.append(_document_from_fetch(link, linked_result, collected_at=collected_at))
            request_count += 1
    source_backed = [document for document in documents if document.get("extraction_status") == "source_backed"]
    record["collection_status"] = "source_backed" if source_backed else str(documents[0].get("extraction_status") or "fetch_error")
    record["source_documents"] = documents
    record["merchant_information"] = {
        "status": record["collection_status"],
        "source_documents": documents,
    }
    return record, request_count


def _missing_uri_record(identity: dict[str, Any], *, collected_at: str) -> dict[str, Any]:
    return {
        **identity,
        "website_uri": "",
        "collected_at": collected_at,
        "collection_status": "source_unavailable",
        "source_documents": [],
        "merchant_information": {"status": "source_unavailable", "source_documents": []},
    }


def _reusable_record(record: Any, identity: dict[str, Any], uri: str) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    candidate = {"name": record.get("name"), "location": record.get("location")}
    try:
        matches = identity_key(candidate) == identity_key(identity)
    except (TypeError, ValueError):
        matches = False
    if not matches or str(record.get("website_uri") or "") != uri:
        return None
    return record


def build_service_detail_cache(
    world: dict[str, Any],
    *,
    map_world_sha256: str,
    fetcher: Callable[[str], dict[str, Any]] = fetch_website,
    existing_cache: dict[str, Any] | None = None,
    retry_failures: bool = False,
    collected_at: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    initial_website_requests: int = 0,
) -> dict[str, Any]:
    """Build a complete, resumable service cache for one frozen map world."""
    places = [place for place in world.get("places") or [] if isinstance(place, dict)]
    city = (world.get("cities") or [{}])[0] if isinstance(world.get("cities"), list) else {}
    timestamp = collected_at or _now()
    existing_by_key = {
        identity_key({"name": record.get("name"), "location": record.get("location")}): record
        for record in (existing_cache or {}).get("records") or []
        if isinstance(record, dict)
        and public_identity({"name": record.get("name"), "location": record.get("location")}) is not None
    }
    records: list[dict[str, Any]] = []
    reused = 0
    fetched = 0
    website_requests = max(0, int(initial_website_requests))

    website_place_count = sum(1 for place in places if website_uri(place))
    without_website_count = len(places) - website_place_count
    processed_places = 0
    processed_website_places = 0
    processed_without_website_places = 0
    status_counts: dict[str, int] = {}

    def report_progress() -> None:
        if progress_callback is not None:
            progress_callback({
                "phase": "collecting",
                "place_count": len(places),
                "processed_places": processed_places,
                "website_place_count": website_place_count,
                "processed_website_places": processed_website_places,
                "without_website_count": without_website_count,
                "processed_without_website_places": processed_without_website_places,
                "website_requests": website_requests,
                "status_counts": dict(status_counts),
            })

    def record_request(uri: str) -> None:
        nonlocal website_requests
        website_requests += 1
        if checkpoint_callback is not None:
            checkpoint_callback({"kind": "request", "uri": str(uri)})
        report_progress()

    report_progress()
    for place in sorted(places, key=lambda item: str(item.get("place_id") or "")):
        uri = website_uri(place)
        identity = public_identity(place)
        if identity is None:
            processed_places += 1
            if uri:
                processed_website_places += 1
            else:
                processed_without_website_places += 1
            status_counts["invalid_identity"] = status_counts.get("invalid_identity", 0) + 1
            report_progress()
            continue
        prior = _reusable_record(existing_by_key.get(identity_key(identity)), identity, uri)
        prior_status = str((prior or {}).get("collection_status") or "")
        created_record = False
        if prior and (not retry_failures or prior_status == "source_backed"):
            records.append(prior)
            reused += 1
        elif not uri:
            records.append(_missing_uri_record(identity, collected_at=timestamp))
            created_record = True
        else:
            record_request(uri)
            result = fetcher(uri)
            record, _requests = _record_from_fetch(
                identity, uri, result, collected_at=timestamp, fetcher=fetcher,
                request_callback=record_request,
            )
            records.append(record)
            fetched += 1
            created_record = True

        if created_record and checkpoint_callback is not None:
            checkpoint_callback({"kind": "record", "record": records[-1]})

        processed_places += 1
        if uri:
            processed_website_places += 1
        else:
            processed_without_website_places += 1
        record = records[-1]
        status = str(record.get("collection_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        report_progress()
    return {
        "schema_version": SERVICE_DETAIL_CACHE_SCHEMA,
        "world_binding": {
            "city_id": str(city.get("city_id") or ""),
            "map_world_sha256": map_world_sha256,
            "source_place_count": len(places),
            "source_place_identity_signature": place_identity_signature(places),
        },
        "collected_at": timestamp,
        "records": records,
        "collection_summary": {
            "records": len(records), "reused": reused, "fetched": fetched,
            "website_requests": website_requests,
            "status_counts": status_counts,
        },
    }


def validate_service_detail_cache(cache: dict[str, Any], world: dict[str, Any], *, map_world_sha256: str) -> list[str]:
    """Return all lineage and full-POI-universe violations for a cache."""
    errors: list[str] = []
    places = [place for place in world.get("places") or [] if isinstance(place, dict)]
    city = (world.get("cities") or [{}])[0] if isinstance(world.get("cities"), list) else {}
    binding = cache.get("world_binding") if isinstance(cache.get("world_binding"), dict) else {}
    if cache.get("schema_version") != SERVICE_DETAIL_CACHE_SCHEMA:
        errors.append("invalid_schema_version")
    if str(binding.get("city_id") or "") != str(city.get("city_id") or ""):
        errors.append("city_id_mismatch")
    if str(binding.get("map_world_sha256") or "") != map_world_sha256:
        errors.append("map_world_hash_mismatch")
    if int(binding.get("source_place_count") or -1) != len(places):
        errors.append("source_place_count_mismatch")
    if str(binding.get("source_place_identity_signature") or "") != place_identity_signature(places):
        errors.append("source_place_identity_signature_mismatch")
    expected = {identity_key(identity) for place in places if (identity := public_identity(place)) is not None}
    actual = {
        identity_key({"name": record.get("name"), "location": record.get("location")})
        for record in cache.get("records") or []
        if isinstance(record, dict)
        and public_identity({"name": record.get("name"), "location": record.get("location")}) is not None
    }
    if expected != actual:
        errors.append("service_record_identity_set_mismatch")
    return errors


def build_local_service_world_state(cache: dict[str, Any], world: dict[str, Any] | None = None) -> dict[str, Any]:
    """Project every cache record into LocalServiceApp without internal IDs."""
    binding = dict(cache.get("world_binding") or {})
    from shared.opening_hours import effective_opening_hours
    hours_by_identity = {
        identity_key(identity): effective_opening_hours(place)
        for place in (world or {}).get("places", [])
        if (identity := public_identity(place)) is not None
    }
    services = []
    for record in cache.get("records") or []:
        if not isinstance(record, dict):
            continue
        identity = public_identity(record)
        if identity is None:
            continue
        services.append({
            **identity,
            "merchant_information": dict(record.get("merchant_information") or {}),
            **({"opening_hours": hours_by_identity[identity_key(identity)]}
               if hours_by_identity.get(identity_key(identity)) else {}),
        })
    services.sort(key=identity_key)
    payload = {
        "schema_version": LOCAL_SERVICE_WORLD_STATE_SCHEMA,
        "world_binding": binding,
        "services": services,
    }
    payload["service_detail_cache_sha256"] = hashlib.sha256(
        json.dumps(cache, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def service_detail_cache_metadata(path: str | Path) -> dict[str, Any]:
    """Return compact registry metadata without treating a missing cache as valid."""
    cache_path = Path(path)
    if not cache_path.is_file():
        return {}
    cache = read_json(cache_path)
    binding = cache.get("world_binding") if isinstance(cache.get("world_binding"), dict) else {}
    summary = cache.get("collection_summary") if isinstance(cache.get("collection_summary"), dict) else {}
    return {
        "city_id": str(binding.get("city_id") or ""),
        "map_world_sha256": str(binding.get("map_world_sha256") or ""),
        "source_place_count": int(binding.get("source_place_count") or 0),
        "records": len(cache.get("records") or []),
        "status_counts": dict(summary.get("status_counts") or {}),
    }


def write_service_detail_snapshot(
    world_path: str | Path,
    *,
    cache_path: str | Path | None = None,
    state_path: str | Path | None = None,
    fetcher: Callable[[str], dict[str, Any]] = fetch_website,
    resume: bool = True,
    retry_failures: bool = False,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Materialize cache and LocalService world state beside one map world."""
    source = Path(world_path)
    world = read_json(source)
    map_world_sha256 = sha256_file(source)
    cache_file = Path(cache_path) if cache_path else source.parent / "service_detail_cache.json"
    state_file = Path(state_path) if state_path else source.parent / "local_service_world_state.json"
    checkpoint_file = cache_file.with_name("service_detail_cache.checkpoint.jsonl")
    if not resume:
        cache_file.unlink(missing_ok=True)
        state_file.unlink(missing_ok=True)
    checkpoint = _prepare_service_detail_checkpoint(
        checkpoint_file, world, map_world_sha256=map_world_sha256, resume=resume,
    )
    existing = read_json(cache_file) if resume and cache_file.is_file() else {}
    existing_records = {
        identity_key(identity): record
        for record in (existing.get("records") or [])
        if isinstance(record, dict) and (identity := public_identity(record)) is not None
    }
    for record in checkpoint.get("records") or []:
        identity = public_identity(record)
        if identity is not None:
            existing_records[identity_key(identity)] = record
    resumable_cache = {"records": list(existing_records.values())}
    cache = build_service_detail_cache(
        world,
        map_world_sha256=map_world_sha256,
        fetcher=fetcher,
        existing_cache=resumable_cache,
        retry_failures=retry_failures,
        progress_callback=progress_callback,
        checkpoint_callback=lambda event: _append_checkpoint_event(checkpoint_file, event),
        initial_website_requests=int(checkpoint.get("website_requests") or 0),
    )
    errors = validate_service_detail_cache(cache, world, map_world_sha256=map_world_sha256)
    if errors:
        raise ValueError(f"Refusing to write invalid service detail cache: {errors}")
    state = build_local_service_world_state(cache, world)
    if progress_callback is not None:
        progress_callback({"phase": "materializing"})
    write_json(cache_file, cache)
    write_json(state_file, state)
    return {
        "cache_path": str(cache_file), "state_path": str(state_file),
        "checkpoint_path": str(checkpoint_file), "cache": cache, "state": state,
    }
