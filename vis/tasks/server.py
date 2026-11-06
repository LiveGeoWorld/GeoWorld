"""Task workbench HTTP adapter. Task files remain authoritative."""
from __future__ import annotations

import json
import hashlib
import mimetypes
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).parent / "static"


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def local_path(root: Path, name: str) -> Path:
    path = (root / name.replace("\\", "/")).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("The requested path is outside the data directory.")
    return path


def batches(root: Path, params: dict):
    selected = []
    for path in root.glob("*/*/*/*"):
        if not (path / "tasks").is_dir():
            continue
        task_type, world, city, batch = path.relative_to(root).parts
        if any(params.get(key) and params[key] != value for key, value in
               (("task_type", task_type), ("world", world), ("city", city))):
            continue
        metadata = read(path / "batch_metadata.json") if (path / "batch_metadata.json").is_file() else {}
        created = str(metadata.get("created_at") or "")
        if not created and (path / "benchmark_manifest.json").is_file():
            created = str(read(path / "benchmark_manifest.json").get("generated_at") or "")
        selected.append((created or batch, path))
    return sorted(selected, key=lambda row: (row[0], str(row[1])), reverse=True)


def manifest(root: Path, params: dict):
    selected, seen = [], set()
    for created, batch in batches(root, params):
        group = batch.relative_to(root).parts[:3]
        requested = params.get("batch", "latest")
        if requested == "latest":
            if group in seen:
                continue
            seen.add(group)
        elif batch.name != requested:
            continue
        selected.append((created, batch))
    tasks, batch_index = [], {}
    for created, batch in selected:
        task_type, world, city, batch_id = batch.relative_to(root).parts
        for path in sorted((batch / "tasks").iterdir()):
            if not (path / "task.json").is_file():
                continue
            task = read(path / "task.json")
            translation_path = path / "display_translation.json"
            if translation_path.is_file():
                translation=read(translation_path)
                if translation.get("source_user_prompt")==task.get("user_prompt"):
                    task = {**task, "user_prompt": translation["user_prompt"], "prompt_language": "en"}
            summary = task.get("summary") or {}
            statuses = read(batch / "review_status.json") if (batch / "review_status.json").is_file() else {}
            tasks.append({"folder": path.name, "folder_ref": path.relative_to(root).as_posix(),
                          "task_id": task.get("task_id", path.name), "task_type": task_type,
                          "city_id": city, "world_id": world, "batch_ts": batch_id,
                          "query_preview": task.get("user_prompt", "")[:80], "summary": summary,
                          "review_status": (statuses.get(path.name) or {}).get("status", summary.get("review_status", "")),
                          "prompt_language": task.get("prompt_language") or summary.get("prompt_language", "")})
        batch_index.setdefault(task_type, {})[city] = {"batch_ts": batch_id, "created_at": created}
    return {"combined": True, "world_id": params.get("world", ""), "tasks": tasks,
            "total_task_count": len(tasks), "task_types": sorted({t["task_type"] for t in tasks}),
            "cities": sorted({t["city_id"] for t in tasks}), "batches": batch_index}


def task_folder(root: Path, folder: str) -> Path:
    if not folder:
        raise ValueError("Select a task first.")
    path = local_path(root, folder)
    if not (path / "task.json").is_file():
        raise FileNotFoundError("Task not found.")
    return path


def translate_display(value, mapping):
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [translate_display(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: translate_display(item, mapping) for key, item in value.items()}
    return value


def task_detail(root: Path, folder: str):
    path = task_folder(root, folder)
    names = ("task", "env_events", "judge_sequence", "world_slice", "task_sandbox", "route_sandbox",
             "grounding_gold", "discovery_gold", "curated_source", "task_provenance")
    result = {name: read(path / (name + ".json")) for name in names if (path / (name + ".json")).is_file()}
    result["folder"] = folder
    review_path=path.parent.parent / "review_status.json"
    if review_path.is_file():
        result["review_status"]=(read(review_path).get(path.name) or {}).get("status", "")
    result["task"]["folder_ref"] = folder
    translation_path = path / "display_translation.json"
    if translation_path.is_file():
        result["display_translation"] = read(translation_path)
        if result["display_translation"].get("source_user_prompt")==result["task"].get("user_prompt"):
            result["task"]["source_user_prompt"] = result["task"]["user_prompt"]
            result["task"]["user_prompt"] = result["display_translation"]["user_prompt"]
    projection = path / "workbench_projection.json"
    if projection.is_file():
        projected=read(projection)
        expected=projected.pop('source_sandbox_sha256', '')
        sandbox_path=path / 'task_sandbox.json'
        if expected and (not sandbox_path.is_file() or hashlib.sha256(sandbox_path.read_bytes()).hexdigest()!=expected):
            projected.pop('task_sandbox',None)
        result.update(projected)
    task_type = result["task"].get("task_type")
    if task_type == "dynamic_replanning":
        result["dynamic_review"] = {name: read(path / (name + ".json")) for name in
                                    ("counterfactual_proof", "dynamic_private_audit", "counterfactual_proof_request", "task_provenance")
                                    if (path / (name + ".json")).is_file()}
        result["dynamic_review"]["places"] = (result.get("world_slice") or {}).get("places", [])
        source = path / "generation_log" / "source.json"
        if source.is_file():
            saved = read(source)
            result["dynamic_review"]["source_sandbox"] = saved.get("gt", {})
            result["dynamic_review"]["places"] = (saved.get("_p1_city_context") or {}).get("places", result["dynamic_review"]["places"])
    if task_type == "dynamic_replanning" and projection.is_file():
        result["dynamic_review"].update(read(projection).get("dynamic_review") or {})
    for name in ("public_fact_projection", "candidate_choice_verification"):
        evidence = path / "generation_log" / (name + ".json")
        if evidence.is_file():
            result[name] = read(evidence)
    translation = result.get("display_translation") or {}
    if translation.get("string_map"):
        for key in ("task_sandbox", "world_slice", "env_events", "dynamic_review"):
            if key in result:
                result[key] = translate_display(result[key], translation["string_map"])
    return result


def send(handler, value, status=200):
    body = json.dumps(value, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def send_file(handler, path: Path):
    if os.name == "nt":
        path = Path("\\\\?\\" + str(path.resolve()))
    if not path.is_file():
        raise FileNotFoundError("File not found.")
    body = path.read_bytes()
    handler.send_response(200)
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    handler.send_header("Content-Type", content_type + ("; charset=utf-8" if path.suffix in {".html", ".js", ".css"} else ""))
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(body)


def handle_api(handler, method: str, endpoint: str, params: dict):
    root = ROOT / "outputs" / "benchmarks"
    if not any(root.glob("*/*/*/*/tasks/*/task.json")):
        root = ROOT / "examples" / "workbench" / "benchmarks"
    contracts = read(Path(__file__).with_name("interfaces.json"))
    contract = next((row for row in contracts if row["method"] == method and row["path"] == endpoint), None)
    if contract is None:
        send(handler, {"error": "Unknown workbench endpoint."}, 404)
        return
    if method == "GET" and endpoint.startswith("/api/app-content/"):
        from vis.tasks.app_http import AppHTTP
        app_root=ROOT / 'outputs' / 'benchmarks'
        if not any(app_root.glob('*/'+str(getattr(handler,'active_world_id',''))+'/*/*/tasks/*/task.json')):
            app_root=ROOT / 'examples' / 'app_runtime' / 'benchmarks'
        AppHTTP(handler, app_root).handle(endpoint, {k: [v] for k, v in params.items()})
        return
    if endpoint in {'/api/generate','/api/generate/status','/api/generate/cancel'}:
        from vis.tasks.generation_http import GenerationHTTP
        GenerationHTTP(handler,ROOT/'outputs/benchmarks',params).handle(method,endpoint)
        return
    if contract["status"] != "connected":
        send(handler, {"error": "Pending migration and integration.", "status": "not_connected",
                       "feature": contract["feature"]}, 501)
        return
    if contract['feature'] in {'map','review'}:
        from vis.tasks.workbench_http import WorkbenchHTTP
        WorkbenchHTTP(handler,root,params).handle(method,endpoint)
        return
    available_worlds = sorted({path.relative_to(root).parts[1] for _, path in batches(root, {})})
    active_world = getattr(handler, "active_world_id", "") or ""
    if "world" not in params and root == ROOT / "examples" / "workbench" / "benchmarks" and active_world not in available_worlds:
        params["world"] = available_worlds[0] if len(available_worlds) == 1 else ""
    params.setdefault("world", active_world)
    if not params["world"] and available_worlds:
        params["world"] = available_worlds[0]
    if contract['feature'] == 'evaluation':
        from vis.tasks.evaluation_http import EvaluationHTTP
        EvaluationHTTP(handler, root, params).handle(method, endpoint)
        return
    if endpoint == "/api/config":
        send(handler, {"google_maps_api_key": "", "preferred_basemap": "osm", "model_options": {"generation": __import__("vis.tasks.evaluation_http", fromlist=["_model_options"])._model_options()["generation"], "testing": __import__("vis.tasks.evaluation_http", fromlist=["_model_options"])._model_options()["testing"]},
                       "generation_task_types": ["place_grounding"], "generation_feasibility": {"mixed_task_types": []}, "workbench_worlds": available_worlds,
                       "active_task_world": params["world"],
                       "connected_features": ["browse", "app", "evaluation", "map", "review"]})
    elif endpoint == "/api/manifest":
        send(handler, manifest(root, params))
    elif endpoint == "/api/cities":
        cities = sorted({path.parent.name for _, path in batches(root, params)})
        from vis.tasks.app_http import AppHTTP
        registered=AppHTTP(handler,root)._current_world_cities()
        options={city:{"city_id":city,"label":city.replace("_", " ").title()} for city in cities}
        options.update({city['city_id']:city for city in registered})
        send(handler,list(options.values()))
    elif endpoint == "/api/task_types":
        send(handler, manifest(root, params)["task_types"])
    elif endpoint == "/api/batches":
        rows = [{"name": path.name, "created_at": created, "display_name": "Task batch",
                 "is_latest": index == 0, "task_count": len(list((path / "tasks").glob("*/task.json")))}
                for index, (created, path) in enumerate(batches(root, params))]
        send(handler, {"batches": rows, "current": rows[0]["name"] if rows else ""})
    elif endpoint == "/api/task":
        send(handler, task_detail(root, params.get("folder", "")))
    elif endpoint == "/api/task/files":
        folder = task_folder(root, params.get("folder", ""))
        send(handler, {p.stem: read(p) for p in folder.glob("*.json")})
    elif endpoint == "/api/asset":
        folder = task_folder(root, params.get("folder", ""))
        path = local_path(folder, params.get("path", ""))
        send_file(handler, path)


def serve_tasks(handler, method: str = "GET") -> bool:
    parsed = urlsplit(handler.path)
    if parsed.path == "/world-api/map-scope" and method == "GET":
        send(handler, handler.map_scope or {"scope": "city", "world_id": handler.active_world_id})
        return True
    if parsed.path == "/tasks":
        handler.send_response(302)
        handler.send_header("Location", "/tasks/")
        handler.end_headers()
        return True
    if not parsed.path.startswith("/tasks/"):
        return False
    try:
        if parsed.path.startswith("/tasks/api/"):
            params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            handle_api(handler, method, parsed.path.removeprefix("/tasks"), params)
        elif method == "GET":
            relative = "index.html" if parsed.path == "/tasks/" else unquote(parsed.path.removeprefix("/tasks/static/"))
            send_file(handler, local_path(STATIC, relative))
        else:
            send(handler, {"error": "Method not allowed."}, 405)
    except FileNotFoundError as exc:
        send(handler, {"error": str(exc)}, 404)
    except (ValueError, KeyError, TypeError) as exc:
        send(handler, {"error": str(exc)}, 400)
    return True
