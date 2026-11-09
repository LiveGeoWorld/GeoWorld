"""Itinerary task visualizer + GT review server.

Shows multi-day itinerary with POIs on map, GT timeline with commute info,
validator audit warnings, menus, generation stats, and full GT editing.

Usage:
    python -m task_module.task_types.itinerary.vis_server \
        --tasks outputs/benchmarks/itinerary/tokyo_complex/latest/tasks/ \
        --world outputs/mapseeds/tokyo_complex/base/world.json \
        --routes outputs/routes/tokyo_complex/route_cache.json \
        --port 8772

If --routes is omitted, commute times are shown from task_sandbox.route_data
(without polylines). With --routes, real road polylines are rendered.
"""

from __future__ import annotations

import json
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[5]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.io import read_json, write_json


class ItineraryVisHandler(SimpleHTTPRequestHandler):
    tasks_dir: str = ""
    task_list: list[dict] = []
    world_data: dict = {}
    route_cache_entries: list[dict] = []
    review_path: str = ""

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        from shared.web_i18n import serve_ui_asset
        if serve_ui_asset(self):
            return
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        try:
            if path == "/api/tasks":
                self._serve_tasks()
            elif path == "/api/task":
                folder = params.get("folder", [""])[0]
                self._serve_task(folder)
            elif path == "/api/routes":
                from_id = params.get("from", [""])[0]
                to_id = params.get("to", [""])[0]
                self._serve_routes(from_id, to_id)
            elif path == "/api/review_status":
                self._serve_review_status()
            elif path == "/" or path == "":
                self._serve_static("index.html")
            elif path.startswith("/static/"):
                self._serve_static(path[8:])
            else:
                super().do_GET()
        except Exception as exc:
            self._error(str(exc), 500)

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_len)) if content_len > 0 else {}
        path = urlparse(self.path).path
        try:
            if path == "/api/task/save":
                self._handle_save(body)
            elif path == "/api/task/review":
                self._handle_review(body)
            else:
                self._error("unknown", 404)
        except Exception as exc:
            self._error(str(exc), 500)

    def _serve_tasks(self) -> None:
        reviews = {}
        if self.review_path and Path(self.review_path).exists():
            reviews = read_json(self.review_path)
        tasks_out = []
        for t in self.task_list:
            r = reviews.get(t["folder"], {})
            t["review_status"] = r.get("status", "")
            t["reviewer"] = r.get("reviewer", "")
            tasks_out.append(t)
        self._json({"tasks": tasks_out})

    def _serve_task(self, folder: str) -> None:
        task_dir = Path(self.tasks_dir) / folder
        if not task_dir.exists():
            self._error("Not found", 404)
            return

        result = {"folder": folder}
        for fname in ["task.json", "task_sandbox.json"]:
            fp = task_dir / fname
            if fp.exists():
                result[fname.replace(".json", "")] = read_json(fp)

        sandbox = result.get("task_sandbox", {})
        sandbox["_poi_details"] = self._build_poi_details(sandbox)
        sandbox["_route_connectors"] = self._build_connectors(sandbox)
        sandbox["_gen_summary"] = self._build_gen_summary(task_dir)

        self._json(result)

    def _build_poi_details(self, sandbox: dict) -> list[dict]:
        items: list[dict] = []
        hotel = sandbox.get("hotel", {})
        if hotel:
            items.append(_poi_card(hotel, "hotel"))
        all_pois = sandbox.get("all_pois", [])
        if isinstance(all_pois, list):
            for p in all_pois:
                items.append(_poi_card(p, p.get("category", "other")))
        else:
            for cat, cat_pois in all_pois.items():
                for p in cat_pois:
                    items.append(_poi_card(p, cat))
        return items

    def _build_connectors(self, sandbox: dict) -> list[dict]:
        """Build a list of connector objects for each consecutive event pair in the GT.

        Each connector has the two place_ids, names, commute info from route_data,
        and optionally polyline data from route_cache.
        """
        itinerary = sandbox.get("correct_itinerary", {})
        route_src = sandbox.get("route_data", {})
        connectors: list[dict] = []
        for day in itinerary.get("days", []):
            events = day.get("events", [])
            for i in range(1, len(events)):
                prev = events[i - 1]
                curr = events[i]
                prev_pid = prev.get("place_id", "")
                curr_pid = curr.get("place_id", "")
                prev_name = prev.get("place_name", "?")
                curr_name = curr.get("place_name", "?")

                conn: dict = {
                    "day": day.get("day", "?"),
                    "from_pid": prev_pid, "to_pid": curr_pid,
                    "from_name": prev_name, "to_name": curr_name,
                }

                commute_info = _lookup_commute(prev_name, curr_name, route_src)
                if commute_info:
                    conn["commute"] = commute_info
                else:
                    conn["commute"] = prev.get("commute_mode", "") or "?"

                cache_info = _lookup_cache(prev_pid, curr_pid, self.route_cache_entries)
                if cache_info:
                    conn["polyline"] = cache_info

                connectors.append(conn)
        return connectors

    def _build_gen_summary(self, task_dir: Path) -> dict:
        gen_dir = task_dir / "generation_log"
        details = {}
        dp = gen_dir / "details.json"
        if dp.exists():
            details = read_json(dp)
        g = details.get("generation", {})
        return {
            "model": g.get("llm_model", "?"),
            "tokens_prompt": g.get("tokens_prompt", 0),
            "tokens_completion": g.get("tokens_completion", 0),
            "tokens_total": g.get("tokens_total", 0),
            "llm_duration_s": g.get("llm_duration_s", 0),
            "route_api_calls": g.get("route_api_calls", 0),
            "route_cache_hits": g.get("route_cache_hits", 0),
            "audit_warnings": g.get("audit_warnings", 0) or details.get("audit_warnings", 0),
            "num_days": details.get("num_days", "?"),
            "total_pois": details.get("total_pois", "?"),
            "gt_event_count": (details.get("gt_summary") or {}).get("gt_event_count", "?"),
        }

    def _serve_routes(self, from_id: str, to_id: str) -> None:
        modes = _lookup_cache(from_id, to_id, self.route_cache_entries)
        self._json({"routes": modes})

    def _serve_review_status(self) -> None:
        if self.review_path and Path(self.review_path).exists():
            self._json(read_json(self.review_path))
        else:
            self._json({})

    def _handle_save(self, body: dict) -> None:
        folder = body.get("folder", "")
        sandbox_path = Path(self.tasks_dir) / folder / "task_sandbox.json"
        task_path = Path(self.tasks_dir) / folder / "task.json"
        if not sandbox_path.exists():
            self._error("task_sandbox.json not found", 404)
            return
        sandbox = read_json(sandbox_path)
        for key in ("expected_feasible", "correct_itinerary",
                     "character", "hotel", "user_prompt"):
            if key in body:
                sandbox[key] = body[key]
        if "constraints" in body:
            for k, v in body["constraints"].items():
                sandbox.setdefault("constraints", {})[k] = v
        if "user_prompt" in body and task_path.exists():
            td = read_json(task_path)
            td["user_prompt"] = body["user_prompt"]
            write_json(task_path, td)
        write_json(sandbox_path, sandbox)
        self._json({"status": "saved"})

    def _handle_review(self, body: dict) -> None:
        folder = body.get("folder", "")
        status = body.get("status", "")
        if not folder or status not in ("approved", "rejected"):
            self._error("invalid", 400)
            return
        rp = Path(self.review_path) if self.review_path else Path(self.tasks_dir).parent / "review_status.json"
        reviews = read_json(rp) if rp.exists() else {}
        reviews[folder] = {
            "status": status,
            "reviewer": body.get("reviewer", ""),
            "notes": body.get("notes", ""),
            "reviewed_at": body.get("reviewed_at", ""),
        }
        write_json(rp, reviews)
        self._json({"status": "ok"})

    def _serve_static(self, filename: str) -> None:
        static_dir = Path(__file__).parent / "vis"
        fp = static_dir / filename
        if not fp.exists():
            self._error(f"Not found: {filename}", 404)
            return
        ct = "text/html"
        if filename.endswith(".js"): ct = "application/javascript"
        elif filename.endswith(".css"): ct = "text/css"
        self.send_response(200)
        self.send_header("Content-Type", f"{ct}; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(fp.read_bytes())

    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, msg, code=500):
        body = json.dumps({"error": msg}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def _poi_card(p: dict, cat: str) -> dict:
    loc = p.get("location", {})
    rating = p.get("rating")
    return {
        "place_id": p.get("place_id", ""),
        "name": p.get("name", ""),
        "category": cat,
        "district": p.get("district_name", p.get("district", "")),
        "rating": round(rating, 1) if rating else None,
        "price_level": p.get("price_level", ""),
        "opening_hours": p.get("opening_hours", ""),
        "duration_min": p.get("duration_typical_min", p.get("duration_min", 60)),
        "lat": loc.get("lat", 0),
        "lng": loc.get("lng", 0),
    }


def _lookup_commute(name_a: str, name_b: str,
                     route_src: dict[str, dict]) -> dict | None:
    a = name_a[:15]
    b = name_b[:15]
    for key, modes in route_src.items():
        if a in key and b in key:
            times = {m: t for m, t in modes.items() if isinstance(t, (int, float))}
            if times:
                return {"key": key, "modes": times}
    return None


def _lookup_cache(pid_a: str, pid_b: str,
                   entries: list[dict]) -> dict | None:
    if not entries:
        return None
    for r in entries:
        oid = r.get("origin_place_id", "")
        did = r.get("dest_place_id", "")
        if (oid == pid_a and did == pid_b) or (oid == pid_b and did == pid_a):
            return {
                "mode": r.get("mode", ""),
                "distance_m": r.get("distance_m", 0),
                "duration_min": r.get("duration_s", 0) // 60,
                "polyline_encodings": r.get("polyline_encodings", []),
            }
    return None


def _load_route_cache(path: str) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    data = read_json(path)
    entries = []
    for r in data.get("cache", []):
        route = r.get("route", {})
        if route.get("available") is False:
            continue
        steps = route.get("steps", [])
        polys = [s.get("polyline", {}).get("encodedPolyline", "")
                 for s in steps if s.get("polyline", {}).get("encodedPolyline")]
        entries.append({
            "route_id": r.get("route_id", ""),
            "origin_place_id": r.get("origin_place_id", ""),
            "origin_name": r.get("origin_name", ""),
            "dest_place_id": r.get("dest_place_id", ""),
            "dest_name": r.get("dest_name", ""),
            "mode": r.get("mode", ""),
            "distance_m": route.get("distance_m", 0),
            "duration_s": route.get("duration_s", 0),
            "polyline_encodings": polys,
        })
    return entries


def main():
    import argparse
    p = argparse.ArgumentParser(description="Itinerary task visualizer")
    p.add_argument("--tasks", required=True)
    p.add_argument("--world", default="")
    p.add_argument("--routes", default="")
    p.add_argument("--port", type=int, default=8772)
    args = p.parse_args()

    tasks_dir = Path(args.tasks)
    if not tasks_dir.exists():
        print(f"Error: {tasks_dir} not found")
        return 1

    world_data = read_json(args.world) if args.world and Path(args.world).exists() else {}
    route_entries = _load_route_cache(args.routes)

    task_list = []
    for folder in sorted(tasks_dir.iterdir()):
        if not folder.is_dir():
            continue
        tf = folder / "task.json"
        if tf.exists():
            tk = read_json(tf)
            task_list.append({
                "folder": folder.name,
                "task_id": tk.get("task_id", ""),
                "user_prompt": tk.get("user_prompt", "")[:80],
                "summary": tk.get("summary", {}),
            })

    review_path = str(Path(args.tasks).parent / "review_status.json")

    ItineraryVisHandler.tasks_dir = str(tasks_dir)
    ItineraryVisHandler.task_list = task_list
    ItineraryVisHandler.world_data = world_data
    ItineraryVisHandler.route_cache_entries = route_entries
    ItineraryVisHandler.review_path = review_path

    server = HTTPServer(("0.0.0.0", args.port), ItineraryVisHandler)
    print(f"[ItineraryVis] http://localhost:{args.port}")
    print(f"[ItineraryVis] {len(task_list)} tasks  |  {len(route_entries)} cached routes")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[ItineraryVis] Shutting down")
        server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
