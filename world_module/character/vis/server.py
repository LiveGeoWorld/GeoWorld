"""Character relationship graph visualization.

Start the server:
    python3 -m world_module.character.vis.server \\
        --input outputs/characters/tokyo_complex/demo/characters_validation.json \\
        --port 8766

Then open http://localhost:8766
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.io import read_json


class VisHandler(SimpleHTTPRequestHandler):
    characters_data: dict = {}
    input_path: str = ""

    def do_GET(self):
        from shared.web_i18n import serve_ui_asset
        if serve_ui_asset(self):
            return
        path = self.path

        try:
            if path == "/api/data":
                self._json(self.characters_data)
            elif path == "/" or path == "/index.html":
                self._serve_static("index.html")
            elif path == "/app.js":
                self._serve_static("app.js")
            elif path == "/style.css":
                self._serve_static("style.css")
            else:
                self._serve_static("index.html")
        except Exception as exc:
            self._error(str(exc), 500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, filename: str):
        static_dir = Path(__file__).resolve().parent / "static"
        file_path = static_dir / filename
        if not file_path.exists():
            self._error(f"Not found: {filename}", 404)
            return
        content = file_path.read_bytes()
        ct = "text/html"
        if filename.endswith(".js"):
            ct = "application/javascript"
        elif filename.endswith(".css"):
            ct = "text/css"
        self.send_response(200)
        self.send_header("Content-Type", f"{ct}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _error(self, msg: str, code: int = 500):
        body = json.dumps({"error": msg}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        if "/api/" in str(args):
            super().log_message(format, *args)


def make_server(input_path: str, port: int = 8766) -> HTTPServer:
    data = read_json(input_path) if input_path else {}
    VisHandler.characters_data = data
    VisHandler.input_path = input_path
    return HTTPServer(("0.0.0.0", port), VisHandler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Character Relationship Graph Viewer")
    parser.add_argument("--input", required=True, help="Path to characters.json")
    parser.add_argument("--port", type=int, default=8766, help="Server port (default: 8766)")
    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"Error: file not found: {args.input}")
        sys.exit(1)

    server = make_server(args.input, args.port)
    print(f"==============================================")
    print(f"  Character Relationship Graph")
    print(f"  Data: {args.input}")
    print(f"  Open: http://localhost:{args.port}")
    print(f"==============================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()
