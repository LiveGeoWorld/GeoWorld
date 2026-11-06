"""Google Street View retrieval for grounding batch generation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from shared.maps_client import GoogleMapsClient
from task_module.task_types.place_grounding.batch_generation.config import pipeline_root


METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
IMAGE_URL = "https://maps.googleapis.com/maps/api/streetview"


@dataclass
class StreetViewRequest:
    lat: float
    lng: float
    target_id: str
    output_dir: str
    headings: list[int]
    size: str = "640x640"
    fov: int = 80
    pitch: int = 0
    radius_m: int = 60


def fetch_streetview_assets(
    request: StreetViewRequest,
    api_key: str | None = None,
    progress_callback: Callable[[str, str, str], None] | None = None,
) -> list[dict[str, Any]]:
    """Fetch Street View images around a gold coordinate.

    Returns image asset records for the evidence ledger. Failures are returned as
    records with ``quality=fetch_failed`` so quality gate can reject them.
    """
    if api_key:
        raise ValueError("Custom Street View api_key is unsupported; use the configured Google Maps rotation.")
    client = GoogleMapsClient()
    root = pipeline_root()
    out_dir = root / request.output_dir / safe_stem(request.target_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    _emit_progress(progress_callback, "streetview_metadata", "running")
    try:
        metadata = client.street_view_metadata(request.lat, request.lng, request.radius_m)
    except Exception as exc:  # noqa: BLE001 - preserve fetch failure in evidence
        metadata = {"status": "REQUEST_FAILED", "error": str(exc)}
    if metadata.get("status") != "OK":
        _emit_progress(progress_callback, "streetview_metadata", "failed")
        return [
            {
                "role": "evaluation",
                "path": "",
                "source": "google_streetview_static",
                "quality": "fetch_failed",
                "error": metadata.get("status", "metadata_failed"),
                "metadata": metadata,
                "gold_coordinate": {"lat": request.lat, "lng": request.lng},
            }
        ]

    _emit_progress(progress_callback, "streetview_metadata", "complete")
    assets = []
    pano_location = metadata.get("location") or {}
    for index, heading in enumerate(request.headings):
        filename = f"{safe_stem(request.target_id)}_h{heading}.jpg"
        path = out_dir / filename
        _emit_progress(
            progress_callback,
            "streetview_image",
            "running",
            f"{index + 1}/{len(request.headings)}",
        )
        try:
            path.write_bytes(client.download_street_view(
                request.lat, request.lng, heading=heading, pitch=request.pitch,
                fov=request.fov, size=request.size,
            ))
            quality = "candidate"
            error = ""
            _emit_progress(
                progress_callback,
                "streetview_image",
                "complete",
                f"{index + 1}/{len(request.headings)}",
            )
        except Exception as exc:  # noqa: BLE001 - preserve fetch failure in ledger
            quality = "fetch_failed"
            error = str(exc)
            _emit_progress(
                progress_callback,
                "streetview_image",
                "failed",
                f"{index + 1}/{len(request.headings)}",
            )
        assets.append(
            {
                "role": _streetview_asset_role(index),
                "path": str(path.relative_to(root)) if path.exists() else "",
                "source": "google_streetview_static",
                "capture_date": metadata.get("date", ""),
                "viewpoint": {
                    "heading": heading,
                    "fov": request.fov,
                    "pitch": request.pitch,
                    "size": request.size,
                    "radius_m": request.radius_m,
                    "pano_id": metadata.get("pano_id", ""),
                    "pano_location": pano_location,
                },
                "gold_coordinate": {"lat": request.lat, "lng": request.lng},
                "quality": quality,
                "error": error,
                "metadata": metadata,
                "google_request_telemetry": client.telemetry_snapshot(),
            }
        )
    return assets


def _emit_progress(
    callback: Callable[[str, str, str], None] | None,
    step: str,
    state: str,
    note: str = "",
) -> None:
    if callback is not None:
        callback(step, state, note)


def _streetview_asset_role(index: int) -> str:
    """Separate final evaluation image from prompt-writing reference images."""
    if index == 0:
        return "evaluation"
    if index == 1:
        return "prompt_reference"
    return "candidate"


def safe_stem(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value or "target"))
    return cleaned[:80] or "target"
