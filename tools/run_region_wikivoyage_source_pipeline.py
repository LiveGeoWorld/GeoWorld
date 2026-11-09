"""Run the author-side Wikivoyage source preparation pipeline for one city.

This writes source assets only. It never creates a Region task or formal batch.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import time
from mimetypes import guess_extension
from pathlib import Path
from typing import Any

import requests
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.config_loader import PipelineConfig
from shared.costing import apply_llm_pricing_reference
from task_module.task_types.region_aware.wikivoyage_ingestor import WikivoyageIngestor
from task_module.task_types.region_aware.wikivoyage_source_pipeline import (
    build_poi_type_catalog,
    build_stage1_request,
    build_source_chunks,
    materialize_regional_rules,
    build_stage2_assessment_request,
    materialize_stage2_assessments,
    merge_stage2_assessment_entries,
    stage2_source_rules,
    PIPELINE_CONTRACT_VERSION,
    validate_llm_output,
    write_json_atomic,
)


class LLMOutputProtocolError(RuntimeError):
    """The endpoint responded, but its JSON contract could not be materialized."""

    def __init__(self, message: str, *, raw_output: str | None = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output


class MediaFetchError(RuntimeError):
    """Classify one media failure without exposing transport details to the scheduler."""

    def __init__(self, message: str, *, phase: str, source_type: str, retryable: bool) -> None:
        super().__init__(message)
        self.phase = phase
        self.source_type = source_type
        self.retryable = retryable


_SOURCE_FILE_API = "https://en.wikivoyage.org/w/api.php"
_COMMONS_FILE_API = "https://commons.wikimedia.org/w/api.php"
_RETRYABLE_MEDIA_STATUS_CODES = {429, 500, 502, 503, 504}
_WIKIMEDIA_THUMB_WIDTHS = (1280, 960, 640, 512, 320, 250, 160)
_MAX_MEDIA_BYTES = 8 * 1024 * 1024
_MEDIA_SUCCESS_INTERVAL_S = 30.0
_MODEL_RETRY_DELAYS = (5, 15)
_ITEM_CACHE_PREFIXES = {
    "stage1_extraction": "s1",
    "stage2_assessment": "s2",
}


def _new_call_budget(max_calls: int) -> int | None:
    """Represent zero as unlimited and positives as remaining new calls."""
    return None if max_calls == 0 else max_calls


def _consume_call_budget(call_budget: int | None) -> int | None:
    return None if call_budget is None else call_budget - 1


def _is_transient_model_infrastructure_error(exc: Exception) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout, TimeoutError)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


def _is_retryable_media_error(exc: Exception) -> bool:
    if isinstance(exc, (
        requests.ConnectionError, requests.Timeout,
        requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError,
    )):
        return True
    return (
        isinstance(exc, requests.HTTPError)
        and exc.response is not None
        and exc.response.status_code in _RETRYABLE_MEDIA_STATUS_CODES
    )


def _short_item_filename(stage: str, item_id: str, *, failed: bool = False) -> str:
    """Return a stable short physical name while preserving the logical ID in JSON."""
    prefix = _ITEM_CACHE_PREFIXES.get(stage, "item")
    digest = hashlib.sha256(f"{stage}\0{item_id}".encode("utf-8")).hexdigest()[:16]
    suffix = ".failed.json" if failed else ".json"
    return f"{prefix}_{digest}{suffix}"


def _item_paths(directory: Path, stage: str, item_id: str, *, failed: bool = False) -> tuple[Path, Path]:
    """Return the new short path followed by the legacy logical-ID path."""
    suffix = ".failed.json" if failed else ".json"
    return directory / _short_item_filename(stage, item_id, failed=failed), directory / f"{item_id}{suffix}"


def _load_item_json(directory: Path, stage: str, item_id: str) -> tuple[dict[str, Any] | None, Path]:
    short_path, legacy_path = _item_paths(directory, stage, item_id)
    for candidate in (short_path, legacy_path):
        cached = _load_json(candidate)
        if cached is not None:
            return cached, candidate
    return None, short_path


def _media_get(
    session: requests.Session, url: str, *, params: dict[str, Any] | None = None,
    timeout: float, max_attempts: int = 4, on_retry: Any = None, stream: bool = False,
) -> requests.Response:
    """Fetch one source asset with bounded retry for rate limits and transient failures."""
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    response: requests.Response | None = None
    for attempt in range(max_attempts):
        try:
            request_kwargs: dict[str, Any] = {"params": params, "timeout": timeout}
            if stream:
                request_kwargs["stream"] = True
            response = session.get(url, **request_kwargs)
        except (requests.ConnectionError, requests.Timeout):
            if attempt + 1 >= max_attempts:
                raise
            delay_s = float(2 ** attempt)
            if on_retry is not None:
                on_retry(attempt + 1, max_attempts, delay_s)
            time.sleep(delay_s)
            continue
        if response.status_code not in _RETRYABLE_MEDIA_STATUS_CODES:
            response.raise_for_status()
            return response
        if attempt + 1 < max_attempts:
            if response.status_code == 429:
                delay_s = (60.0, 90.0, 120.0)[attempt]
            else:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay_s = float(retry_after) if retry_after is not None else float(2 ** attempt)
                except ValueError:
                    delay_s = float(2 ** attempt)
                delay_s = min(max(delay_s, 0.0), 15.0)
            if on_retry is not None:
                on_retry(attempt + 1, max_attempts, delay_s)
            time.sleep(delay_s)
    assert response is not None
    response.raise_for_status()
    raise AssertionError("unreachable")


def _media_blocks(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        block for section in snapshot.get("sections", []) for block in section.get("blocks", [])
        if isinstance(block, dict) and block.get("block_type") == "media"
    ]


def _lookup_image_info(session: requests.Session, reference: str, on_retry: Any = None) -> tuple[dict[str, Any], str, str]:
    """Resolve the source wiki's local file before falling back to Commons."""
    for api_url in (_SOURCE_FILE_API, _COMMONS_FILE_API):
        found_file = False
        original_only: tuple[dict[str, Any], str, str] | None = None
        for width in _WIKIMEDIA_THUMB_WIDTHS:
            params = {
                "action": "query", "format": "json", "prop": "imageinfo",
                "iiprop": "url|mime", "iiurlwidth": width, "titles": f"File:{reference}",
            }
            response = _media_get(session, api_url, params=params, timeout=30, on_retry=on_retry)
            pages = ((response.json().get("query") or {}).get("pages") or {})
            for page in pages.values():
                image_info = next(
                    (info for info in (page.get("imageinfo") or []) if isinstance(info, dict)),
                    None,
                )
                if not isinstance(image_info, dict) or not image_info.get("url"):
                    continue
                found_file = True
                thumb_url = str(image_info.get("thumburl") or "")
                if not thumb_url:
                    return (
                        image_info, api_url, str(page.get("imagerepository") or ""),
                    )
                if thumb_url and "thumbnail_unscaled" not in thumb_url:
                    return image_info, api_url, str(page.get("imagerepository") or "")
                original_only = (
                    image_info, api_url, str(page.get("imagerepository") or ""),
                )
            if not found_file:
                break
        if original_only is not None:
            return original_only
        if found_file:
            raise RuntimeError(
                f"Wikimedia did not provide a resized thumbnail for File:{reference}; "
                "the original asset will not be fetched because direct original-image requests may be rate limited"
            )
    raise RuntimeError(
        f"Neither the source Wikivoyage file repository nor Wikimedia Commons has a downloadable raster "
        f"for File:{reference}"
    )


def _read_bounded_response(response: requests.Response, *, reference: str) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_MEDIA_BYTES:
                raise RuntimeError(f"Image exceeds the 8 MiB Stage 1 limit: File:{reference}")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > _MAX_MEDIA_BYTES:
            raise RuntimeError(f"Image exceeds the 8 MiB Stage 1 limit: File:{reference}")
        chunks.append(chunk)
    if total == 0:
        raise RuntimeError(f"Image payload is empty: File:{reference}")
    return b"".join(chunks)


def _download_original(
    session: requests.Session, url: str, *, reference: str, on_retry: Any = None,
    max_attempts: int = 4,
) -> tuple[bytes, requests.Response]:
    """Download an official original with whole-transfer retries and a hard size limit."""
    for attempt in range(max_attempts):
        try:
            response = _media_get(session, url, timeout=45, max_attempts=1, stream=True)
            mime_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
            if mime_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                raise MediaFetchError(
                    f"Wikimedia did not provide a vision-compatible original for File:{reference}: "
                    f"{mime_type or 'unknown'}",
                    phase="original_validation", source_type="wikimedia_original", retryable=False,
                )
            return _read_bounded_response(response, reference=reference), response
        except MediaFetchError:
            raise
        except Exception as exc:
            if not _is_retryable_media_error(exc) or attempt + 1 >= max_attempts:
                raise
            if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 429:
                delay_s = (60.0, 90.0, 120.0)[attempt]
            else:
                delay_s = float(2 ** attempt)
            if on_retry is not None:
                on_retry(attempt + 1, max_attempts, delay_s)
            time.sleep(delay_s)
    raise AssertionError("unreachable")


def _resize_original_image(image_bytes: bytes, mime_type: str, *, reference: str) -> tuple[bytes, str]:
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
            image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            if mime_type == "image/png" and image.mode in {"RGBA", "LA"}:
                image.save(output, format="PNG", optimize=True)
                return output.getvalue(), "image/png"
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image.save(output, format="JPEG", quality=90, optimize=True)
            return output.getvalue(), "image/jpeg"
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Wikimedia original is not a usable raster: File:{reference}") from exc


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    """Replace one binary cache asset only after its complete bytes reach disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _media_data_url(
    root: Path, media_block: dict[str, Any], *, before_network_download: Any = None,
) -> tuple[str, dict[str, Any], bool]:
    """Resolve a Wikivoyage file once and retain a content-addressed local raster."""
    media_id = str(media_block.get("media_id") or "")
    reference = str(media_block.get("file_reference") or "").strip()
    if not media_id or not reference:
        raise ValueError("Media block is missing media_id or file_reference")
    cache_root = root / "media"
    metadata_path = root / "_work" / "media_fetch" / f"{media_id}.json"
    cached = _load_json(metadata_path)
    if cached is not None:
        local_path = root / str(cached.get("local_asset_path") or "")
        if local_path.is_file():
            raw = local_path.read_bytes()
            mime_type = str(cached.get("mime_type") or "")
            if raw and mime_type.startswith("image/"):
                return f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}", cached, False

    if before_network_download is not None:
        before_network_download()

    session = requests.Session()
    session.headers.update({
        "User-Agent": "GeoAgentBench/1.0 (https://github.com/PKQZPC/GeoWorld; research)",
        "Accept": "application/json, image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8,*/*;q=0.5",
    })
    def report_retry(attempt: int, max_attempts: int, delay_s: float) -> None:
        print(
            f"media_retry {attempt}/{max_attempts} {delay_s:g} {json.dumps(reference, ensure_ascii=False)}",
            flush=True,
        )

    try:
        image_info, file_api_url, image_repository = _lookup_image_info(session, reference, report_retry)
    except Exception as exc:
        raise MediaFetchError(
            f"Wikimedia image-info lookup failed for File:{reference}: {exc}",
            phase="image_info", source_type="wikimedia_api",
            retryable=_is_retryable_media_error(exc),
        ) from exc
    thumbnail_url = str(image_info.get("thumburl") or "")
    original_url = str(image_info["url"])
    resolved_url = thumbnail_url or original_url
    download_source = "wikimedia_thumbnail" if thumbnail_url else "original_fallback"
    original_bytes: int | None = None
    if thumbnail_url:
        try:
            image_response = _media_get(session, thumbnail_url, timeout=45, on_retry=report_retry)
            image_bytes = image_response.content
        except Exception as exc:
            if not _is_retryable_media_error(exc):
                raise MediaFetchError(
                    f"Wikimedia thumbnail download failed for File:{reference}: {exc}",
                    phase="thumbnail", source_type="wikimedia_thumbnail", retryable=False,
                ) from exc
            try:
                image_bytes, image_response = _download_original(
                    session, original_url, reference=reference, on_retry=report_retry,
                )
            except MediaFetchError:
                raise
            except Exception as original_exc:
                raise MediaFetchError(
                    f"Wikimedia original fallback failed for File:{reference}: {original_exc}",
                    phase="original_download", source_type="wikimedia_original",
                    retryable=_is_retryable_media_error(original_exc),
                ) from original_exc
            download_source = "original_fallback"
            resolved_url = original_url
    else:
        try:
            image_bytes, image_response = _download_original(
                session, original_url, reference=reference, on_retry=report_retry,
            )
        except MediaFetchError:
            raise
        except Exception as exc:
            raise MediaFetchError(
                f"Wikimedia original download failed for File:{reference}: {exc}",
                phase="original_download", source_type="wikimedia_original",
                retryable=_is_retryable_media_error(exc),
            ) from exc
    if download_source == "original_fallback":
        original_bytes = len(image_bytes)
    if not image_bytes or len(image_bytes) > _MAX_MEDIA_BYTES:
        raise RuntimeError(f"Image payload is empty or exceeds the 8 MiB Stage 1 limit: File:{reference}")
    mime_type = str(image_response.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
    if mime_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        raise RuntimeError(f"Wikimedia did not provide a vision-compatible raster for File:{reference}: {mime_type or 'unknown'}")
    if download_source == "original_fallback":
        image_bytes, mime_type = _resize_original_image(image_bytes, mime_type, reference=reference)
    extension = guess_extension(mime_type) or ".img"
    relative_path = Path("media") / f"{media_id}{extension}"
    local_path = root / relative_path
    cache_root.mkdir(parents=True, exist_ok=True)
    _write_bytes_atomic(local_path, image_bytes)
    metadata = {
        "media_id": media_id,
        "file_reference": reference,
        "file_api_url": file_api_url,
        "image_repository": image_repository,
        "source_url": str(image_info["url"]),
        "resolved_url": resolved_url,
        "mime_type": mime_type,
        "local_asset_path": relative_path.as_posix(),
        "sha256": hashlib.sha256(image_bytes).hexdigest(),
        "bytes": len(image_bytes),
        "download_source": download_source,
    }
    if original_bytes is not None:
        metadata["original_bytes"] = original_bytes
    write_json_atomic(metadata_path, metadata)
    return f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}", metadata, True


def _record_media_fetch_failure(
    *, root: Path, run_meta: dict[str, Any], media_block: dict[str, Any],
    media_completed: int, media_total: int, exc: Exception,
) -> None:
    media_id = str(media_block.get("media_id") or "unknown_media")
    retryable = isinstance(exc, MediaFetchError) and exc.retryable
    failure = {
        "stage": "media_fetch",
        "item_id": media_id,
        "file_reference": str(media_block.get("file_reference") or ""),
        "error_type": type(exc).__name__,
        "error": str(exc),
        "failure_kind": "infrastructure" if retryable else "deterministic",
        "retryable": retryable,
        "media_phase": exc.phase if isinstance(exc, MediaFetchError) else "media_processing",
        "source_type": exc.source_type if isinstance(exc, MediaFetchError) else "local_processing",
        "recorded_at_epoch_s": round(time.time(), 3),
    }
    write_json_atomic(root / "_work" / "media_fetch" / f"{media_id}.failed.json", failure)
    run_meta.setdefault("failures", []).append(failure)
    _write_progress(root, run_meta, "media_fetch", media_completed, media_total)


def call_json(
    endpoint: dict[str, Any], request: dict[str, Any], *, max_retries: int,
    call_log_path: Path | None = None, call_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call an OpenAI-compatible JSON endpoint and preserve every complete attempt."""
    base_url = str(endpoint.get("base_url") or "").rstrip("/")
    if not base_url:
        raise ValueError("Configured model endpoint has no base_url")
    last_error: Exception | None = None
    repair_feedback: str | None = None
    call_log = {
        "schema_version": "geoword.region_source_model_call.v1",
        "context": dict(call_context or {}),
        "endpoint_name": str(endpoint.get("endpoint_name") or ""),
        "model": str(endpoint.get("model") or ""),
        "base_url": base_url,
        "attempts": [],
    }
    for attempt in range(max_retries + 1):
        started = time.monotonic()
        started_epoch_s = time.time()
        user_content: str | list[dict[str, Any]] = json.dumps(request["input"], ensure_ascii=False)
        image_data_urls = list(request.get("image_data_urls") or [])
        if image_data_urls:
            user_content = [{"type": "text", "text": user_content}]
            user_content.extend(
                {"type": "image_url", "image_url": {"url": image_data_url}}
                for image_data_url in image_data_urls
            )
        messages = [
            {"role": "system", "content": request["system_prompt"]},
            {"role": "user", "content": user_content},
        ] + ([{"role": "user", "content": repair_feedback}] if repair_feedback else [])
        request_payload = {
            "model": endpoint.get("model", ""),
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "max_tokens": _completion_token_limit(endpoint, request),
        }
        attempt_record: dict[str, Any] = {
            "attempt": attempt + 1,
            "started_at_epoch_s": round(started_epoch_s, 3),
            "request": request_payload,
            "status": "request_started",
        }
        call_log["attempts"].append(attempt_record)
        if call_log_path is not None:
            write_json_atomic(call_log_path, call_log)
        try:
            session = requests.Session()
            session.trust_env = False
            response = session.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {endpoint.get('api_key', '')}", "Content-Type": "application/json"},
                json=request_payload,
                timeout=float(endpoint.get("request_timeout_seconds", 90)),
            )
            attempt_record["http_status"] = response.status_code
            attempt_record["raw_response_text"] = response.text
            try:
                payload = response.json()
            except ValueError:
                payload = None
            attempt_record["raw_response"] = payload
            response.raise_for_status()
            if not isinstance(payload, dict):
                raise LLMOutputProtocolError("LLM response is not a JSON object", raw_output=response.text)
            content = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content")
            if not isinstance(content, str):
                raise LLMOutputProtocolError("LLM response has no message content", raw_output=response.text)
            try:
                parsed = _parse_json(content)
                validate_llm_output(request, parsed)
            except (json.JSONDecodeError, ValueError) as exc:
                raise LLMOutputProtocolError(f"Invalid JSON response: {exc}", raw_output=content) from exc
            usage = payload.get("usage") or {}
            usage_record = {
                "elapsed_s": round(time.monotonic() - started, 3),
                "prompt_tokens": int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0),
                "completion_tokens": int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0),
                "total_tokens": int(usage.get("total_tokens", 0) or 0),
                "attempt": attempt + 1,
            }
            cost = apply_llm_pricing_reference({
                "token_usage": {
                    "prompt": usage_record["prompt_tokens"],
                    "completion": usage_record["completion_tokens"],
                    "total": usage_record["total_tokens"],
                    "cached_input": int(usage.get("cached_input_tokens", 0) or 0),
                },
                "request_usage": [{
                    "prompt": usage_record["prompt_tokens"],
                    "completion": usage_record["completion_tokens"],
                    "total": usage_record["total_tokens"],
                    "cached_input": int(usage.get("cached_input_tokens", 0) or 0),
                }],
            }, endpoint.get("pricing_reference"))
            usage_record["cost"] = cost
            attempt_record.update({
                "status": "validated",
                "elapsed_s": usage_record["elapsed_s"],
                "parsed_output": parsed,
                "usage": usage_record,
            })
            call_log["status"] = "validated"
            call_log["completed_at_epoch_s"] = round(time.time(), 3)
            if call_log_path is not None:
                write_json_atomic(call_log_path, call_log)
            return parsed, usage_record
        except Exception as exc:  # Retry transport and malformed JSON alike.
            last_error = exc
            attempt_record.update({
                "status": "failed",
                "elapsed_s": round(time.monotonic() - started, 3),
                "error_type": type(exc).__name__,
                "error": str(exc),
            })
            if isinstance(exc, LLMOutputProtocolError):
                repair_feedback = (
                    "Previous output failed local JSON validation. Return one complete replacement JSON object only. "
                    "Do not add commentary, external knowledge, source text, IDs, or fields not requested. "
                    f"Validation error: {exc}"
                )
                attempt_record["raw_model_content"] = exc.raw_output
                attempt_record["next_repair_feedback"] = repair_feedback
            call_log["status"] = "retry_pending" if attempt < max_retries else "failed"
            if call_log_path is not None:
                write_json_atomic(call_log_path, call_log)
            if attempt >= max_retries:
                break
            if _is_transient_model_infrastructure_error(exc):
                delay = _MODEL_RETRY_DELAYS[min(attempt, len(_MODEL_RETRY_DELAYS) - 1)]
                item_id = str((call_context or {}).get("item_id") or "")
                print(
                    f"model_retry {attempt + 2}/{max_retries + 1} {delay} "
                    f"{json.dumps(item_id, ensure_ascii=False)}",
                    flush=True,
                )
                time.sleep(delay)
    if isinstance(last_error, LLMOutputProtocolError):
        raise LLMOutputProtocolError(
            f"Author-side LLM response violated the JSON contract after {max_retries + 1} attempts: {last_error}",
            raw_output=last_error.raw_output,
        ) from last_error
    raise RuntimeError(f"Author-side LLM request failed after {max_retries + 1} attempts: {last_error}")


def call_json_hard_timeout(
    endpoint: dict[str, Any], request: dict[str, Any], *, max_retries: int,
    hard_timeout_seconds: float, call_log_path: Path | None = None,
    call_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Enforce a wall-clock deadline even when an HTTP intermediary keeps a socket open."""
    if hard_timeout_seconds <= 0:
        return call_json(
            endpoint, request, max_retries=max_retries,
            call_log_path=call_log_path, call_context=call_context,
        )
    context = multiprocessing.get_context("spawn")
    handle, result_name = tempfile.mkstemp(prefix="region_llm_", suffix=".json")
    os.close(handle)
    result_path = Path(result_name)
    worker = context.Process(target=_call_json_worker, args=(
        str(result_path), endpoint, request, max_retries,
        str(call_log_path) if call_log_path is not None else None, call_context,
    ))
    worker.start()
    worker.join(hard_timeout_seconds)
    if worker.is_alive():
        worker.terminate()
        worker.join()
        result_path.unlink(missing_ok=True)
        raise TimeoutError(f"Author-side LLM wall-clock timeout after {hard_timeout_seconds} seconds")
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        kind = payload["kind"]
    except Exception as exc:
        raise RuntimeError(f"Author-side LLM worker exited without a result (exit code {worker.exitcode})") from exc
    finally:
        result_path.unlink(missing_ok=True)
    if kind == "ok":
        return payload["result"]
    error_type, error = payload["error_type"], payload["error"]
    if error_type == "LLMOutputProtocolError":
        raise LLMOutputProtocolError(error)
    raise RuntimeError(error)


def _call_json_worker(
    result_name: str, endpoint: dict[str, Any], request: dict[str, Any], max_retries: int,
    call_log_name: str | None, call_context: dict[str, Any] | None,
) -> None:
    path = Path(result_name)
    try:
        write_json_atomic(path, {"kind": "ok", "result": call_json(
            endpoint, request, max_retries=max_retries,
            call_log_path=Path(call_log_name) if call_log_name else None,
            call_context=call_context,
        )})
    except Exception as exc:
        write_json_atomic(path, {"kind": "error", "error_type": type(exc).__name__, "error": str(exc)})


def run(args: argparse.Namespace) -> dict[str, Any]:
    cfg = PipelineConfig(args.config)
    endpoint_name = cfg.stage_model(args.stage)
    endpoint = dict(cfg.model_endpoint(endpoint_name))
    endpoint["endpoint_name"] = endpoint_name
    vision_endpoint_name = cfg.stage_model(args.vision_stage)
    vision_endpoint = dict(cfg.model_endpoint(vision_endpoint_name))
    vision_endpoint["endpoint_name"] = vision_endpoint_name
    if not vision_endpoint.get("supports_vision"):
        raise ValueError(f"Configured Stage 1 vision endpoint does not support images: {vision_endpoint_name}")
    if args.request_timeout_seconds:
        endpoint["request_timeout_seconds"] = args.request_timeout_seconds
        vision_endpoint["request_timeout_seconds"] = args.request_timeout_seconds
    endpoint["hard_timeout_seconds"] = args.hard_timeout_seconds
    vision_endpoint["hard_timeout_seconds"] = args.hard_timeout_seconds
    if args.max_completion_tokens is not None:
        endpoint["max_completion_tokens"] = args.max_completion_tokens
    root = Path(args.output_root)
    if args.stage2_only:
        return _run_stage2_only(args, root, endpoint, endpoint_name)
    if args.run_stage2:
        raise ValueError("Stage 2 must be started in a separate reviewed invocation with --stage2-only.")
    print("region_phase source_read", flush=True)
    ingestor = WikivoyageIngestor(args.dump, str(Path(args.output_root) / "legacy_unused"))
    snapshot = ingestor.extract_city_source_snapshot(args.page_title, city_id=args.city_id)
    if snapshot is None:
        raise RuntimeError(f"Wikivoyage page not found: {args.page_title}")

    print("region_phase source_parse", flush=True)
    previous_manifest = _load_json(root / "run_manifest.json")
    if previous_manifest and (
        previous_manifest.get("source_page_hash") not in {None, snapshot["source"]["page_hash"]}
        or previous_manifest.get("pipeline_contract_version") not in {None, PIPELINE_CONTRACT_VERSION}
    ):
        raise RuntimeError(
            "The selected output root belongs to a different source or pipeline contract. "
            "Use a new output root; cached Stage 1/2 artifacts must never be mixed."
        )
    write_json_atomic(root / "wikivoyage_source_snapshot.json", snapshot)
    chunks = build_source_chunks(
        snapshot,
        max_source_chars=args.max_source_request_chars,
        max_list_items=args.max_list_items_per_request,
        max_atomic_source_chars=args.max_atomic_source_chars,
    )
    work_root = root / "_work"
    run_meta = previous_manifest or {
        "world_id": args.world_id,
        "city_id": args.city_id,
        "page_title": args.page_title,
        "source_page_hash": snapshot["source"]["page_hash"],
        "pipeline_contract_version": PIPELINE_CONTRACT_VERSION,
        "endpoint": endpoint_name,
        "model": endpoint.get("model", ""),
        "vision_endpoint": vision_endpoint_name,
        "vision_model": vision_endpoint.get("model", ""),
        "chunks": len(chunks),
        "request_plan": {
            "max_source_chars": args.max_source_request_chars,
            "max_list_items": args.max_list_items_per_request,
            "max_atomic_source_chars": args.max_atomic_source_chars,
        },
        "calls": [],
    }
    manifest_changed = _migrate_failure_ledger(run_meta)
    manifest_changed = _reconcile_manifest_calls_from_logs(work_root, run_meta) or manifest_changed
    manifest_changed = _reconcile_resolved_failures(work_root, run_meta) or manifest_changed
    if manifest_changed:
        run_meta["totals"] = _usage_totals(run_meta)
        write_json_atomic(root / "run_manifest.json", run_meta)
    call_budget = _new_call_budget(args.max_calls)

    # Resolve and cache every image before its containing slice is sent once to
    # the multimodal Stage-1 model. Raw Wikitext and media provenance remain in
    # the immutable source snapshot.
    media_data_urls: dict[str, str] = {}
    media_metadata: dict[str, dict[str, Any]] = {}
    media_blocks = _media_blocks(snapshot)
    last_network_media_success: float | None = None

    def throttle_network_media() -> None:
        if last_network_media_success is None:
            return
        remaining_s = _MEDIA_SUCCESS_INTERVAL_S - (time.monotonic() - last_network_media_success)
        if remaining_s > 0:
            print(f"media_throttle {remaining_s:.1f}", flush=True)
            time.sleep(remaining_s)

    for media_index, media_block in enumerate(media_blocks, start=1):
        media_id = str(media_block["media_id"])
        print(
            f"region_item media_fetch {media_index}/{len(media_blocks)} "
            f"{json.dumps(str(media_block.get('file_reference') or ''), ensure_ascii=False)}",
            flush=True,
        )
        try:
            image_data_url, metadata, downloaded = _media_data_url(
                root, media_block, before_network_download=throttle_network_media,
            )
        except Exception as exc:
            _record_media_fetch_failure(
                root=root, run_meta=run_meta, media_block=media_block,
                media_completed=media_index - 1, media_total=len(media_blocks), exc=exc,
            )
            raise RuntimeError(
                f"media_fetch failed for {media_id}; diagnostics saved under {work_root / 'media_fetch'}. "
                "No contextualized source asset was materialized."
            ) from exc
        media_failure_path = work_root / "media_fetch" / f"{media_id}.failed.json"
        if media_failure_path.exists():
            media_failure_path.unlink()
            _mark_failure_resolved(run_meta, "media_fetch", media_id)
        media_data_urls[media_id] = image_data_url
        media_metadata[media_id] = metadata
        if downloaded:
            last_network_media_success = time.monotonic()
        _write_progress(root, run_meta, "media_fetch", media_index, len(media_blocks))
        print(f"media_fetch {media_index}/{len(media_blocks)}", flush=True)

    print("region_phase stage1_extraction", flush=True)
    stage1_outputs: list[dict[str, Any]] = []
    for chunk in chunks:
        cached, path = _load_item_json(work_root / "stage1_extraction", "stage1_extraction", chunk["chunk_id"])
        if cached is not None:
            stage1_outputs.append(cached)
            continue
        if call_budget is not None and call_budget <= 0:
            return _pause(root, run_meta, "stage1_extraction", chunks, stage1_outputs)
        chunk_images = {
            str(block["media_id"]): media_data_urls[str(block["media_id"])]
            for block in chunk["blocks"] if block.get("block_type") == "media"
        }
        request = build_stage1_request(snapshot, chunk, image_data_urls=chunk_images)
        selected_endpoint = vision_endpoint if chunk_images else endpoint
        output, usage = _run_chunk_call(
            root=root,
            work_root=work_root,
            run_meta=run_meta,
            stage="stage1_extraction",
            item_id=chunk["chunk_id"],
            request=request,
            endpoint=selected_endpoint,
            max_retries=args.max_retries,
            retry_failed=args.retry_failed,
            total_items=len(chunks),
        )
        write_json_atomic(path, output)
        stage1_outputs.append(output)
        run_meta["calls"].append({
            "stage": "stage1_extraction", "chunk_id": chunk["chunk_id"],
            "modality": "multimodal" if chunk_images else "text", **usage,
        })
        call_budget = _consume_call_budget(call_budget)
        _write_progress(root, run_meta, "stage1_extraction", len(stage1_outputs), len(chunks))
        print(f"stage1_extraction {len(stage1_outputs)}/{len(chunks)}", flush=True)
    rules_asset = materialize_regional_rules(snapshot, chunks, stage1_outputs)
    write_json_atomic(root / "regional_rules.json", rules_asset)

    run_meta["totals"] = _usage_totals(run_meta)
    run_meta["result"] = {
        "stage": "stage1_complete",
        "rules": len(rules_asset["rules"]),
        "next_step": "Review Stage 1, then start a separate --stage2-only invocation with the selected frozen world binding.",
    }
    write_json_atomic(root / "run_manifest.json", run_meta)
    return run_meta


def _run_stage2_only(
    args: argparse.Namespace, root: Path, endpoint: dict[str, Any], endpoint_name: str,
) -> dict[str, Any]:
    """Run Stage 2 only after a complete, reviewed Stage 1 asset exists."""
    if not args.world or not args.city_version:
        raise ValueError("--stage2-only requires both --world and --city-version")
    run_meta = _load_json(root / "run_manifest.json")
    snapshot = _load_json(root / "wikivoyage_source_snapshot.json")
    rules_asset = _load_json(root / "regional_rules.json")
    existing_assessments = _load_json(root / "regional_rule_stage2_assessments.json")
    if not run_meta or not snapshot or not rules_asset:
        raise RuntimeError("--stage2-only requires a complete Stage 1 output root")
    _archive_invalid_two_pass_stage2(root, run_meta)
    _archive_invalid_unified_stage2_source_gate(root, run_meta, rules_asset)
    if run_meta.get("pipeline_contract_version") not in {"region_source_pipeline.v7", PIPELINE_CONTRACT_VERSION}:
        raise RuntimeError("Stage 1 asset was produced by a different pipeline contract; use a new Stage 1 run")
    previous_stage = (run_meta.get("result") or {}).get("stage")
    if previous_stage not in {"stage1_complete", "stage2_complete"}:
        raise RuntimeError("Stage 1 is not complete and may not enter Stage 2")
    source_hash = str(snapshot.get("source", {}).get("page_hash") or "")
    if not source_hash or source_hash != str(rules_asset.get("source_page_hash") or ""):
        raise RuntimeError("Stage 1 source assets have inconsistent provenance hashes")
    if str(rules_asset.get("city_id") or "") != str(args.city_id):
        raise RuntimeError("Stage 1 rules do not belong to the requested city")

    print("region_phase type_catalog", flush=True)
    world = _load_json(Path(args.world))
    if world is None:
        raise RuntimeError(f"World JSON is unreadable: {args.world}")
    catalog = build_poi_type_catalog(world)
    if not catalog:
        raise RuntimeError("Frozen world has no places with primary_type values")
    work_root = root / "_work"
    manifest_changed = _reconcile_manifest_calls_from_logs(work_root, run_meta)
    manifest_changed = _reconcile_resolved_failures(work_root, run_meta) or manifest_changed
    if manifest_changed:
        run_meta["totals"] = _usage_totals(run_meta)
        write_json_atomic(root / "run_manifest.json", run_meta)
    call_budget = _new_call_budget(args.max_calls)
    evidence_by_id = {
        str(block["evidence_id"]): {
            "evidence_id": str(block["evidence_id"]),
            "source_text": str(block.get("source_text") or ""),
            "source_kind": str(block.get("block_type") or ""),
        }
        for section in snapshot.get("sections", [])
        for block in section.get("blocks", [])
        if block.get("evidence_id")
    }
    stage2_rules = stage2_source_rules(rules_asset)
    existing_rows = {
        str(row.get("rule_id") or ""): row
        for row in ((existing_assessments or {}).get("assessments") or [])
        if isinstance(row, dict)
    }
    current_rule_ids = {str(rule.get("rule_id") or "") for rule in stage2_rules}
    existing_rows = {rule_id: row for rule_id, row in existing_rows.items() if rule_id in current_rule_ids}
    new_assessment_rows: list[dict[str, Any]] = []
    cached_assessment_count = 0
    new_assessment_call_count = 0
    assessment_work_root = _stage2_cache_dir(work_root, world)
    print("region_phase stage2_assessment", flush=True)
    for rule_index, rule in enumerate(stage2_rules, start=1):
        print(
            f"region_item stage2_assessment {rule_index}/{len(stage2_rules)} "
            f"{json.dumps('恢复已有评估 · ' + str(rule['rule_id']), ensure_ascii=False)}",
            flush=True,
        )
        if rule["rule_id"] in existing_rows:
            cached_assessment_count += 1
            continue
        try:
            evidence = {"blocks": [evidence_by_id[ref] for ref in rule["evidence_refs"]]}
        except KeyError as exc:
            raise RuntimeError(f"Stage 1 context asset is missing cited evidence {exc.args[0]}") from exc
        request = build_stage2_assessment_request(rule, evidence, catalog)
        cached, path = _load_item_json(assessment_work_root, "stage2_assessment", rule["rule_id"])
        if cached is None:
            _call_log, recovered_call_path = _load_item_json(
                work_root / "model_calls" / "stage2_assessment", "stage2_assessment", rule["rule_id"],
            )
            recovered = _recover_validated_call(
                recovered_call_path,
                request=request, endpoint=endpoint, stage="stage2_assessment", item_id=rule["rule_id"],
            )
            if recovered is not None:
                cached, _usage = recovered
        if cached is not None:
            try:
                cached_asset = materialize_stage2_assessments(
                    {**rules_asset, "rules": [rule]}, catalog, [cached],
                    world_id=args.world_id, city_version=args.city_version,
                    frozen_world_hash=_json_hash(world),
                )
            except ValueError:
                cached = None
            else:
                if not path.is_file():
                    write_json_atomic(path, cached)
                new_assessment_rows.extend(cached_asset["assessments"])
                cached_assessment_count += 1
                continue
        if call_budget is not None and call_budget <= 0:
            paused = _pause(root, run_meta, "stage2_assessment", stage2_rules, [*existing_rows.values(), *new_assessment_rows])
            paused["cached_assessment_count"] = cached_assessment_count
            paused["new_assessment_call_count"] = new_assessment_call_count
            return paused
        statement = str(rule.get("statement") or rule.get("rule_id") or "")
        print(
            f"region_item stage2_assessment {rule_index}/{len(stage2_rules)} "
            f"{json.dumps('模型请求 · ' + statement[:160], ensure_ascii=False)}",
            flush=True,
        )
        output, usage = _run_chunk_call(
            root=root, work_root=work_root, run_meta=run_meta, stage="stage2_assessment", item_id=rule["rule_id"],
            request=request, endpoint=endpoint,
            max_retries=args.max_retries, retry_failed=args.retry_failed, total_items=len(stage2_rules),
            completed_before_call=len(existing_rows) + len(new_assessment_rows),
        )
        new_asset = materialize_stage2_assessments(
            {**rules_asset, "rules": [rule]}, catalog, [output],
            world_id=args.world_id, city_version=args.city_version,
            frozen_world_hash=_json_hash(world),
        )
        write_json_atomic(path, output)
        new_assessment_rows.extend(new_asset["assessments"])
        new_assessment_call_count += 1
        call_budget = _consume_call_budget(call_budget)
        run_meta["calls"].append({"stage": "stage2_assessment", "rule_id": rule["rule_id"], **usage})
        completed_count = len(existing_rows) + len(new_assessment_rows)
        _write_progress(root, run_meta, "stage2_assessment", completed_count, len(stage2_rules))
        print(f"stage2_assessment {completed_count}/{len(stage2_rules)}", flush=True)
    print("region_phase asset_generation", flush=True)
    assessments = merge_stage2_assessment_entries(
        rules_asset, catalog, existing_assessments, new_assessment_rows,
        world_id=args.world_id, city_version=args.city_version, frozen_world_hash=_json_hash(world),
    )
    write_json_atomic(root / "regional_rule_stage2_assessments.json", assessments)
    run_meta["totals"] = _usage_totals(run_meta)
    run_meta["result"] = {
        "stage": "stage2_complete",
        "stage1_rules": len(rules_asset["rules"]),
        "stage2_input_rules": len(stage2_rules),
        "task_source_eligible": sum(item["status"] == "task_source_eligible" for item in assessments["assessments"]),
        "cached_assessment_count": cached_assessment_count,
        "new_assessment_call_count": new_assessment_call_count,
    }
    run_meta["stage2_world_binding"] = {
        "world_id": args.world_id,
        "city_id": args.city_id,
        "city_version": args.city_version,
        "frozen_world_hash": _json_hash(world),
    }
    write_json_atomic(root / "run_manifest.json", run_meta)
    return run_meta


def _usage_totals(run_meta: dict[str, Any]) -> dict[str, Any]:
    totals = {
        "requests": len(run_meta["calls"]),
        "prompt_tokens": sum(call["prompt_tokens"] for call in run_meta["calls"]),
        "completion_tokens": sum(call["completion_tokens"] for call in run_meta["calls"]),
        "elapsed_s": round(sum(call["elapsed_s"] for call in run_meta["calls"]), 3),
    }
    estimated_costs: dict[str, float] = {}
    for call in run_meta["calls"]:
        cost = call.get("cost") if isinstance(call.get("cost"), dict) else {}
        amount = cost.get("money_estimate")
        currency = str(cost.get("money_estimate_currency") or "")
        if amount is not None and currency:
            estimated_costs[currency] = estimated_costs.get(currency, 0.0) + float(amount)
    totals["estimated_reference_costs"] = {
        currency: round(amount, 8) for currency, amount in sorted(estimated_costs.items())
    }
    return totals


def _json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _stage2_cache_dir(work_root: Path, world: dict[str, Any]) -> Path:
    """Keep world-bound Stage-2 cache files below the Windows path limit."""
    return work_root / "stage2" / _json_hash(world)[:16]


def _recover_validated_call(
    call_log_path: Path, *, request: dict[str, Any], endpoint: dict[str, Any],
    stage: str, item_id: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Recover a validated response only when its complete request is unchanged."""
    call_log = _load_json(call_log_path)
    if not call_log or call_log.get("status") != "validated":
        return None
    context = call_log.get("context") if isinstance(call_log.get("context"), dict) else {}
    if context.get("stage") != stage or context.get("item_id") != item_id:
        return None
    if (str(call_log.get("endpoint_name") or "") != str(endpoint.get("endpoint_name") or "")
            or str(call_log.get("model") or "") != str(endpoint.get("model") or "")
            or str(call_log.get("base_url") or "").rstrip("/") != str(endpoint.get("base_url") or "").rstrip("/")):
        return None
    expected_user = json.dumps(request["input"], ensure_ascii=False)
    for attempt in reversed(call_log.get("attempts") or []):
        if not isinstance(attempt, dict) or attempt.get("status") != "validated":
            continue
        payload = attempt.get("request") if isinstance(attempt.get("request"), dict) else {}
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        if len(messages) < 2:
            continue
        if payload.get("model") != endpoint.get("model"):
            continue
        if messages[0] != {"role": "system", "content": request["system_prompt"]}:
            continue
        if messages[1] != {"role": "user", "content": expected_user}:
            continue
        output = attempt.get("parsed_output")
        usage = attempt.get("usage")
        if not isinstance(output, dict) or not isinstance(usage, dict):
            continue
        try:
            validate_llm_output(request, output)
        except ValueError:
            continue
        return output, usage
    return None


def _archive_invalid_two_pass_stage2(root: Path, run_meta: dict[str, Any]) -> None:
    """Isolate an old split Stage-2 run and restore its valid Stage-1 boundary.

    This migration is intentionally narrow: it recognizes only the former
    ``eligibility`` plus ``poi_type_binding`` implementation. Formal task
    batches are outside this source-pipeline directory and are never touched.
    """
    old_screening = root / "regional_rule_screening.json"
    old_work = root / "_work"
    old_paths = [
        old_screening,
        root / "stage2.stdout.log",
        root / "stage2.stderr.log",
        old_work / "eligibility",
        old_work / "poi_type_binding",
    ]
    has_old_calls = any(
        call.get("stage") in {"eligibility", "poi_type_binding"}
        for call in run_meta.get("calls") or [] if isinstance(call, dict)
    )
    if not old_screening.exists() and not has_old_calls:
        return

    archive_root = root / "historical_invalid" / f"two_pass_stage2_{time.strftime('%Y%m%d_%H%M%S')}"
    archive_root.mkdir(parents=True, exist_ok=False)
    write_json_atomic(archive_root / "invalid_run_manifest.json", run_meta)
    moved: list[str] = []
    for source in old_paths:
        if not source.exists():
            continue
        target = archive_root / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        moved.append(source.relative_to(root).as_posix())
    write_json_atomic(archive_root / "invalid_artifact_manifest.json", {
        "classification": "historical_invalid_implementation",
        "reason": (
            "This output used the invalid split Stage 2A eligibility and Stage 2B type-binding flow. "
            "It is diagnostic only and must never be used by Stage 3 or formal task generation."
        ),
        "moved_paths": moved,
    })

    run_meta["calls"] = [
        call for call in run_meta.get("calls") or []
        if not isinstance(call, dict) or call.get("stage") not in {"eligibility", "poi_type_binding"}
    ]
    run_meta.pop("stage2_world_binding", None)
    run_meta["result"] = {"stage": "stage1_complete", "archived_invalid_stage2": str(archive_root.relative_to(root))}
    run_meta["totals"] = _usage_totals(run_meta)
    write_json_atomic(root / "run_manifest.json", run_meta)


def _archive_invalid_unified_stage2_source_gate(
    root: Path,
    run_meta: dict[str, Any],
    rules_asset: dict[str, Any],
) -> None:
    """Isolate unified Stage-2 output that admitted non-supported Stage-1 rules."""
    assessment_path = root / "regional_rule_stage2_assessments.json"
    assessment_asset = _load_json(assessment_path)
    if not assessment_asset:
        return

    status_by_rule_id = {
        str(rule.get("rule_id")): str(rule.get("extraction_status") or "")
        for rule in rules_asset.get("rules") or []
        if isinstance(rule, dict) and rule.get("rule_id")
    }
    assessments = [
        item for item in assessment_asset.get("assessments") or []
        if isinstance(item, dict)
    ]
    invalid_rows = [
        item for item in assessments
        if status_by_rule_id.get(str(item.get("rule_id"))) != "supported"
    ]
    if not invalid_rows:
        return

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    archive_root = root / "historical_invalid" / f"unified_stage2_source_gate_{timestamp}"
    archive_root.mkdir(parents=True, exist_ok=False)
    write_json_atomic(archive_root / "invalid_run_manifest.json", run_meta)

    paths = [assessment_path, root / "stage3_inventory.json"]
    paths.extend(sorted(root.glob("stage2_unified*.log")))
    moved: list[str] = []
    for source in paths:
        if not source.exists():
            continue
        target = archive_root / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        moved.append(source.relative_to(root).as_posix())

    invalid_eligible_count = sum(
        item.get("status") == "task_source_eligible" for item in invalid_rows
    )
    retained_cache = root / "_work" / "stage2"
    write_json_atomic(archive_root / "invalid_artifact_manifest.json", {
        "classification": "historical_invalid_implementation",
        "reason": (
            "This unified Stage 2 output incorrectly admitted Stage 1 rules whose "
            "extraction_status was not supported. It is diagnostic only and must "
            "never be used by Stage 3 or formal task generation."
        ),
        "assessment_count": len(assessments),
        "non_supported_assessment_count": len(invalid_rows),
        "non_supported_eligible_count": invalid_eligible_count,
        "moved_paths": moved,
        "retained_cache_path": retained_cache.relative_to(root).as_posix(),
    })

    run_meta["calls"] = [
        call for call in run_meta.get("calls") or []
        if not isinstance(call, dict) or call.get("stage") != "stage2_assessment"
    ]
    run_meta.pop("stage2_world_binding", None)
    archive_reference = archive_root.relative_to(root).as_posix()
    run_meta["result"] = {
        "stage": "stage1_complete",
        "archived_invalid_stage2": archive_reference,
    }
    run_meta.setdefault("historical_invalid_artifacts", []).append({
        "classification": "historical_invalid_implementation",
        "path": archive_reference,
        "reason": "unified_stage2_admitted_non_supported_stage1_rules",
    })
    run_meta["totals"] = _usage_totals(run_meta)
    write_json_atomic(root / "run_manifest.json", run_meta)


def _pause(root: Path, run_meta: dict[str, Any], stage: str, items: list[dict[str, Any]], completed: list[dict[str, Any]]) -> dict[str, Any]:
    _write_progress(root, run_meta, stage, len(completed), len(items))
    return {
        "state": "in_progress",
        "stage": stage,
        "completed": len(completed),
        "total": len(items),
    }


def _run_chunk_call(
    *,
    root: Path,
    work_root: Path,
    run_meta: dict[str, Any],
    stage: str,
    item_id: str,
    request: dict[str, Any],
    endpoint: dict[str, Any],
    max_retries: int,
    retry_failed: bool,
    total_items: int,
    completed_before_call: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one resumable LLM call and preserve a diagnosable failure atomically."""
    failure_path, legacy_failure_path = _item_paths(work_root / stage, stage, item_id, failed=True)
    call_log_path, _legacy_call_log_path = _item_paths(work_root / "model_calls" / stage, stage, item_id)
    existing_failure = next((path for path in (failure_path, legacy_failure_path) if path.exists()), None)
    if existing_failure is not None and not retry_failed:
        completed = (
            int(completed_before_call) if completed_before_call is not None
            else _completed_count(work_root, stage)
        )
        _write_progress(root, run_meta, stage, completed, total_items)
        raise RuntimeError(
            f"Previous {stage} failure exists for {item_id}: {existing_failure}. "
            "Inspect it, then rerun with --retry-failed for an explicit retry."
        )
    started = time.monotonic()
    try:
        output, usage = call_json_hard_timeout(
            endpoint, request, max_retries=max_retries,
            hard_timeout_seconds=float(endpoint.get("hard_timeout_seconds", 0)),
            call_log_path=call_log_path,
            call_context={"stage": stage, "item_id": item_id},
        )
    except Exception as exc:
        failure = {
            "stage": stage,
            "item_id": item_id,
            "endpoint": endpoint.get("endpoint_name"),
            "model": endpoint.get("model"),
            "request_timeout_seconds": endpoint.get("request_timeout_seconds"),
            "max_retries": max_retries,
            "elapsed_s": round(time.monotonic() - started, 3),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "model_call_log_path": str(call_log_path),
            "recorded_at_epoch_s": round(time.time(), 3),
        }
        write_json_atomic(failure_path, failure)
        failure["failure_kind"] = _failure_kind(exc)
        run_meta.setdefault("failures", []).append(failure)
        completed = (
            int(completed_before_call) if completed_before_call is not None
            else _completed_count(work_root, stage)
        )
        _write_progress(root, run_meta, stage, completed, total_items)
        raise RuntimeError(
            f"{stage} failed for {item_id}; diagnostics saved to {failure_path}. "
            "No derived asset was materialized."
        ) from exc
    resolved_failure = False
    for candidate in (failure_path, legacy_failure_path):
        if candidate.exists():
            candidate.unlink()
            resolved_failure = True
    if resolved_failure:
        _mark_failure_resolved(run_meta, stage, item_id)
    return output, usage


def _completed_count(work_root: Path, stage: str) -> int:
    return sum(1 for path in (work_root / stage).glob("*.json") if not path.name.endswith(".failed.json"))


def _manifest_call_identity(call: dict[str, Any]) -> tuple[str, str]:
    stage = str(call.get("stage") or "")
    item_id = str(call.get("chunk_id") or call.get("rule_id") or call.get("item_id") or "")
    return stage, item_id


def _reconcile_manifest_calls_from_logs(work_root: Path, run_meta: dict[str, Any]) -> bool:
    """Restore successful per-item usage that was durable before a manifest interruption."""
    calls = run_meta.setdefault("calls", [])
    known = {
        _manifest_call_identity(call)
        for call in calls if isinstance(call, dict) and all(_manifest_call_identity(call))
    }
    changed = False
    model_calls_root = work_root / "model_calls"
    for stage, id_field in (("stage1_extraction", "chunk_id"), ("stage2_assessment", "rule_id")):
        stage_root = model_calls_root / stage
        for path in sorted(stage_root.glob("*.json")) if stage_root.is_dir() else []:
            call_log = _load_json(path)
            if not call_log or call_log.get("status") != "validated":
                continue
            context = call_log.get("context") if isinstance(call_log.get("context"), dict) else {}
            item_id = str(context.get("item_id") or "")
            if context.get("stage") != stage or not item_id:
                continue
            if (stage, item_id) in known:
                continue
            usage = next((
                attempt.get("usage") for attempt in reversed(call_log.get("attempts") or [])
                if isinstance(attempt, dict) and attempt.get("status") == "validated"
                and isinstance(attempt.get("usage"), dict)
            ), None)
            if usage is None:
                continue
            calls.append({"stage": stage, id_field: item_id, **usage})
            known.add((stage, item_id))
            changed = True
    return changed


def _completion_token_limit(endpoint: dict[str, Any], request: dict[str, Any]) -> int:
    configured = endpoint.get("max_completion_tokens")
    if configured is not None:
        return int(configured)
    item_count = int((request.get("validation") or {}).get("item_count") or 1)
    return min(8192, max(2200, item_count * 300))


def _failure_kind(exc: Exception) -> str:
    return "model_output_protocol" if isinstance(exc, LLMOutputProtocolError) else "infrastructure"


def _migrate_failure_ledger(run_meta: dict[str, Any]) -> bool:
    """Correct manifests written before output-protocol failures had their own class."""
    legacy = run_meta.pop("infrastructure_failures", None)
    if not legacy:
        return False
    ledger = run_meta.setdefault("failures", [])
    for failure in legacy:
        item = dict(failure)
        error = str(item.get("error") or "")
        item["failure_kind"] = "model_output_protocol" if "Unterminated string" in error else "infrastructure"
        ledger.append(item)
    return True


def _reconcile_resolved_failures(work_root: Path, run_meta: dict[str, Any]) -> bool:
    changed = False
    for failure in run_meta.get("failures", []):
        if failure.get("resolved_at_epoch_s") is not None:
            continue
        stage = str(failure.get("stage") or "")
        item_id = str(failure.get("item_id") or "")
        completed_paths = _item_paths(work_root / stage, stage, item_id)
        failed_paths = _item_paths(work_root / stage, stage, item_id, failed=True)
        call_log, _call_path = _load_item_json(work_root / "model_calls" / stage, stage, item_id)
        validated_log = bool(call_log and call_log.get("status") == "validated")
        if (any(path.is_file() for path in completed_paths) or validated_log) and not any(path.exists() for path in failed_paths):
            failure["resolved_at_epoch_s"] = round(time.time(), 3)
            failure["resolution"] = "explicit_retry_succeeded"
            changed = True
    return changed


def _mark_failure_resolved(run_meta: dict[str, Any], stage: str, item_id: str) -> None:
    resolved_at = round(time.time(), 3)
    for failure in run_meta.get("failures", []):
        if failure.get("stage") == stage and failure.get("item_id") == item_id and failure.get("resolved_at_epoch_s") is None:
            failure["resolved_at_epoch_s"] = resolved_at
            failure["resolution"] = "explicit_retry_succeeded"


def _write_progress(root: Path, run_meta: dict[str, Any], stage: str, completed: int, total: int) -> None:
    run_meta["progress"] = {"stage": stage, "completed": completed, "total": total}
    run_meta["totals"] = _usage_totals(run_meta)
    write_json_atomic(root / "run_manifest.json", run_meta)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _parse_json(content: str) -> dict[str, Any]:
    raw = content.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("LLM response must be a JSON object")
    return parsed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world-id", required=True)
    parser.add_argument("--city-id", required=True)
    parser.add_argument("--page-title", required=True)
    parser.add_argument("--dump", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument(
        "--world", default="",
        help="Selected frozen world.json; required only with --stage2-only.",
    )
    parser.add_argument(
        "--city-version", default="",
        help="Active city version; required only with --stage2-only.",
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--stage", default="quality_llm")
    parser.add_argument(
        "--vision-stage", default="vision_parse",
        help="Configured vision-capable author model used for Stage 1 slices containing images.",
    )
    parser.add_argument(
        "--max-source-request-chars", "--max-source-chunk-tokens",
        dest="max_source_request_chars", type=int, default=2400,
        help="Maximum raw-source characters in one Qwen request; lists split only between complete items.",
    )
    parser.add_argument(
        "--max-list-items-per-request", type=int, default=5,
        help="Maximum complete items from one continuous list in one Qwen request.",
    )
    parser.add_argument(
        "--max-atomic-source-chars", type=int, default=8000,
        help="Explicit review limit for one unsplittable paragraph, template record, table, media unit, or list item.",
    )
    parser.add_argument(
        "--run-stage2", action="store_true",
        help="Deprecated and rejected. Review Stage 1, then use a separate --stage2-only invocation.",
    )
    parser.add_argument(
        "--stage2-only", action="store_true",
        help="Run Stage 2 eligibility and POI-type binding against a reviewed, complete Stage 1 asset.",
    )
    parser.add_argument("--max-retries", type=int, default=2, help="Retry count; JSON protocol retries receive local validation feedback.")
    parser.add_argument(
        "--max-calls", type=int, default=8,
        help="Maximum new model calls for this invocation; 0 runs continuously without a call limit.",
    )
    parser.add_argument("--request-timeout-seconds", type=float, default=0)
    parser.add_argument("--hard-timeout-seconds", type=float, default=180)
    parser.add_argument("--max-completion-tokens", type=int, default=None)
    parser.add_argument("--retry-failed", action="store_true", help="Explicitly retry a chunk with a saved failure record.")
    args = parser.parse_args()
    if args.max_calls < 0:
        parser.error("--max-calls must be 0 (unlimited) or a positive integer")
    result = run(args)
    print(json.dumps(result.get("result", result), ensure_ascii=False, indent=2))
