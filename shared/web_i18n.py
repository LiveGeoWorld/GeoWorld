"""Read-only, shared UI assets. No generation or evaluation state is touched."""
from pathlib import Path
from urllib.parse import urlsplit


def serve_ui_asset(handler):
    path = urlsplit(handler.path).path
    if not path.startswith('/ui/'):
        return False
    name = path.removeprefix('/ui/')
    types = {'i18n.js': 'application/javascript', 'en.json': 'application/json',
             'i18n.css': 'text/css'}
    if name not in types:
        handler.send_error(404)
        return True
    body = (Path(__file__).parent / 'web_ui' / name).read_bytes()
    handler.send_response(200)
    handler.send_header('Content-Type', types[name] + '; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'no-cache')
    handler.end_headers()
    handler.wfile.write(body)
    return True
