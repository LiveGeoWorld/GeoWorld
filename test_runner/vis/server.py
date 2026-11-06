"""Test result visualizer — compare GT vs Agent trace + scores + map.

Usage:
    # Browse all runs (with map)
    python -m test_runner.vis.server         --runs outputs/benchmarks/route_planning/shanghai_smoke/latest/runs/         --world outputs/mapseeds/shanghai_smoke/base/world.json         --routes outputs/routes/shanghai_smoke/route_cache.json         --port 8769

    # Single results file (with map)
    python -m test_runner.vis.server         --results path/to/results.json         --world ... --routes ... --port 8769
"""
from __future__ import annotations
import json
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from shared.io import read_json

class ResultHandler(SimpleHTTPRequestHandler):
    results: list[dict] = []
    result_file: str = ''
    runs_dir: str = ''
    runs_list: list[dict] = []
    world_data: dict = {}
    route_cache_data: dict = {}
    route_index: dict[str, dict] = {}
    place_index: dict[str, dict] = {}

    def do_GET(self):
        from shared.web_i18n import serve_ui_asset
        if serve_ui_asset(self):
            return
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        try:
            if path == '/api/results':
                self._json({'results': self.results, 'count': len(self.results)})
            elif path == '/api/runs':
                self._json({'runs': self.runs_list, 'count': len(self.runs_list)})
            elif path.startswith('/api/run/'):
                run_dir = path.split('/api/run/')[1]
                self._serve_run(run_dir)
            elif path == '/api/world':
                self._serve_world()
            elif path == '/api/route_compare':
                self._serve_route_compare(params)
            elif path.startswith('/api/result/'):
                task_id = path.split('/api/result/')[1]
                for r in self.results:
                    if r.get('task_id') == task_id:
                        self._json(r)
                        return
                self._error(f'Task {task_id} not found', 404)
            elif path == '/' or path == '':
                self._serve_static('index.html')
            elif path.startswith('/static/'):
                self._serve_static(path[8:])
            else:
                super().do_GET()
        except Exception as exc:
            self._error(str(exc), 500)

    def _serve_run(self, run_dir_name: str) -> None:
        run_path = Path(self.runs_dir) / run_dir_name / 'results.json'
        if not run_path.exists():
            self._error(f'Run not found: {run_dir_name}', 404)
            return
        ResultHandler.results = read_json(run_path).get('tasks', [])
        ResultHandler.result_file = str(run_path)
        ResultHandler.runs_list = []
        self._json({'results': ResultHandler.results, 'count': len(ResultHandler.results), 'source': str(run_path)})

    def _serve_world(self) -> None:
        places = self.world_data.get('places', [])
        out = [{'place_id': p.get('place_id'), 'name': p.get('name', ''), 'lat': p.get('location', {}).get('lat'), 'lng': p.get('location', {}).get('lng'), 'type': p.get('primary_type', '')} for p in places if p.get('location', {}).get('lat')]
        city = (self.world_data.get('cities') or [{}])[0]
        self._json({'places': out, 'city': city})

    def _serve_route_compare(self, params) -> None:
        """Return GT route + agent route polyline data for map comparison."""
        task_id = params.get('task_id', [''])[0]
        task_data = None
        for r in self.results:
            if r.get('task_id') == task_id:
                task_data = r
                break
        if not task_data:
            self._error(f'Task {task_id} not found in results', 404)
            return
        gt_route_id = task_data.get('gt_route_id', '')
        gt_summary = task_data.get('gt_route_summary') or task_data.get('correct_route_summary') or {}
        core_places = task_data.get('core_places', [])
        result = {'task_id': task_id}
        gt_segments = []
        gt_leg_ids = task_data.get('correct_leg_route_ids') or task_data.get('gt_leg_route_ids') or []
        if gt_leg_ids:
            for (i, rid) in enumerate(gt_leg_ids):
                node = self.route_index.get(rid)
                if node:
                    gt_segments.append({'leg': f'leg_{i + 1}', **node})
        elif gt_route_id:
            has_waypoint = gt_summary.get('has_waypoint', False)
            gt_node = self.route_index.get(gt_route_id)
            if gt_node and (not has_waypoint):
                gt_segments.append({'leg': 'direct', **gt_node})
        if not gt_leg_ids and (not gt_segments):
            waypoint = next((p for p in core_places if p.get('role') == 'waypoint'), None)
            origin_p = next((p for p in core_places if p.get('role') == 'origin'), None)
            dest_p = next((p for p in core_places if p.get('role') == 'destination'), None)
            if waypoint and origin_p and dest_p:
                wp_pid = waypoint.get('place_id', '')
                oid = origin_p.get('place_id', '')
                did = dest_p.get('place_id', '')
                for (rid, node) in self.route_index.items():
                    n_oid = node.get('origin_place_id', '')
                    n_did = node.get('dest_place_id', '')
                    if n_oid == oid and n_did == wp_pid and (node.get('distance_m', 0) > 0):
                        gt_segments.append({'leg': 'origin→waypoint', **node})
                    elif n_oid == wp_pid and n_did == did and (node.get('distance_m', 0) > 0):
                        gt_segments.append({'leg': 'waypoint→dest', **node})
            if not gt_segments and origin_p and dest_p:
                oid = origin_p.get('place_id', '')
                did = dest_p.get('place_id', '')
                for (rid, node) in self.route_index.items():
                    n_oid = node.get('origin_place_id', '')
                    n_did = node.get('dest_place_id', '')
                    if n_oid == oid and n_did == did and (node.get('distance_m', 0) > 0):
                        gt_segments.append({'leg': 'direct', **node})
                        break
        seen = set()
        unique_segments = []
        for seg in gt_segments:
            kid = seg.get('route_id', seg.get('origin_place_id', 'x') + seg.get('dest_place_id', 'x'))
            if kid not in seen:
                seen.add(kid)
                unique_segments.append(seg)
        result['gt_route_segments'] = unique_segments
        fa = task_data.get('final_answer') or {}
        agent_route_ids = fa.get('route_ids') or []
        if not agent_route_ids:
            single_rid = fa.get('route_id', '')
            if single_rid:
                agent_route_ids = [single_rid]
        agent_segments = []
        for rid in agent_route_ids:
            node = self.route_index.get(rid)
            if node:
                agent_segments.append(node)
        if agent_segments:
            result['agent_routes'] = [{'polyline_encodings': n.get('polyline_encodings', []), 'origin_name': n.get('origin_name', ''), 'dest_name': n.get('dest_name', ''), 'mode': n.get('mode', ''), 'distance_m': n.get('distance_m', 0), 'duration_s': n.get('duration_s', 0)} for n in agent_segments]
        pois = []
        for cp in core_places:
            pid = cp.get('place_id', '')
            info = self.place_index.get(pid, {})
            pois.append({'place_id': pid, 'name': cp.get('name', info.get('name', '')), 'role': cp.get('role', ''), 'lat': info.get('lat'), 'lng': info.get('lng')})
        result['core_places'] = pois
        self._json(result)

    def _serve_static(self, filename: str) -> None:
        static_dir = Path(__file__).parent / 'vis_static'
        filepath = static_dir / filename
        if not filepath.exists():
            self._error(f'Not found: {filename}', 404)
            return
        ct = 'text/html'
        if filename.endswith('.js'):
            ct = 'application/javascript'
        elif filename.endswith('.css'):
            ct = 'text/css'
        self.send_response(200)
        self.send_header('Content-Type', f'{ct}; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(filepath.read_bytes())

    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, msg, code=500):
        body = json.dumps({'error': msg}).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

def _build_route_index(cache_path: str) -> dict[str, dict]:
    """Extract route_id → {polyline_encodings, origin/dest latlng} from cache."""
    if not cache_path or not Path(cache_path).exists():
        return {}
    data = read_json(cache_path)
    idx = {}
    for r in data.get('cache', []):
        rid = r.get('route_id', '')
        steps = r.get('route', {}).get('steps', [])
        encodings = []
        for s in steps:
            enc = s.get('polyline', {}).get('encodedPolyline', '')
            if enc:
                encodings.append(enc)
        idx[rid] = {'polyline_encodings': encodings, 'origin_place_id': r.get('origin_place_id', ''), 'origin_name': r.get('origin_name', ''), 'dest_place_id': r.get('dest_place_id', ''), 'dest_name': r.get('dest_name', ''), 'mode': r.get('mode', ''), 'distance_m': r.get('route', {}).get('distance_m', 0), 'duration_s': r.get('route', {}).get('duration_s', 0)}
    return idx

def _build_place_index(world_path: str) -> dict[str, dict]:
    """Build place_id → {lat, lng, name} from world.json."""
    if not world_path or not Path(world_path).exists():
        return {}
    world = read_json(world_path)
    idx = {}
    for p in world.get('places', []):
        loc = p.get('location', {})
        idx[p.get('place_id', '')] = {'lat': loc.get('lat'), 'lng': loc.get('lng'), 'name': p.get('name', '')}
    return idx

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Test result visualizer')
    parser.add_argument('--results', default='', help='Path to results JSON file')
    parser.add_argument('--runs', default='', help='Path to runs/ directory')
    parser.add_argument('--world', default='', help='Path to world.json (for map POI coordinates)')
    parser.add_argument('--routes', default='', help='Path to route_cache.json (for polyline data)')
    parser.add_argument('--port', type=int, default=8769)
    args = parser.parse_args()
    if args.world:
        ResultHandler.world_data = read_json(args.world) if Path(args.world).exists() else {}
        ResultHandler.place_index = _build_place_index(args.world)
    if args.routes:
        ResultHandler.route_cache_data = read_json(args.routes) if Path(args.routes).exists() else {}
        ResultHandler.route_index = _build_route_index(args.routes)
    if args.runs:
        runs_dir = Path(args.runs)
        if not runs_dir.exists():
            print(f'Error: runs directory not found: {runs_dir}', file=sys.stderr)
            return 1
        runs_list = []
        for d in sorted(runs_dir.iterdir(), reverse=True):
            rf = d / 'results.json'
            if rf.exists():
                data = read_json(rf)
                runs_list.append({'run_id': d.name, 'model_name': data.get('model_name', ''), 'model_key': data.get('model_key', ''), 'started_at': data.get('started_at', ''), 'total_duration_s': data.get('total_duration_s', 0), 'task_count': data.get('task_count', 0), 'passed': data.get('passed', 0), 'token_usage': data.get('token_usage', {})})
        ResultHandler.runs_dir = str(runs_dir)
        ResultHandler.runs_list = runs_list
        print(f'[ResultVis] {len(runs_list)} runs at http://localhost:{args.port}')
    elif args.results:
        results_path = Path(args.results)
        if not results_path.exists():
            print(f'Error: results file not found: {results_path}', file=sys.stderr)
            return 1
        data = read_json(results_path)
        ResultHandler.results = data.get('tasks', data) if isinstance(data, dict) else data
        ResultHandler.result_file = str(results_path)
        print(f'[ResultVis] {len(ResultHandler.results)} results at http://localhost:{args.port}')
    else:
        print('Usage: --results <path> or --runs <path>', file=sys.stderr)
        return 1
    server = HTTPServer(('0.0.0.0', args.port), ResultHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[ResultVis] Shutting down')
        server.shutdown()
if __name__ == '__main__':
    raise SystemExit(main())
