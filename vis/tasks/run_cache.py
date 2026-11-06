"""Disposable presentation indexes; source results remain authoritative."""
import hashlib
import json
from pathlib import Path
from threading import RLock

from shared.io import write_json
from .dynamic_fingerprints import stamp

_lock = RLock()
_memory = {}
_version = 'run_browser_index.v1'
_sources = ('run_state.json', 'run_manifest.json', 'results.json', 'results.jsonl', 'run_summary.json')


def revision(run_dir):
    return tuple((name, stamp(Path(run_dir) / name)) for name in _sources)


def cached_view(run_dir, cache_root, build):
    key = str(Path(run_dir).resolve())
    path = Path(cache_root) / (hashlib.sha256(key.encode()).hexdigest() + '.json')
    with _lock:
        for _ in range(2):
            before = revision(run_dir)
            cached = _memory.get(key)
            if cached and cached[0] == before:
                return cached[1]
            if path.is_file():
                try:
                    stored = json.loads(path.read_text(encoding='utf-8'))
                    if (stored.get('schema_version') == _version and stored.get('source') == key
                            and stored.get('revision') == json.loads(json.dumps(before))):
                        _memory[key] = (before, stored['view'])
                        return stored['view']
                except (OSError, ValueError, KeyError):
                    pass
            view = build(Path(run_dir))
            if revision(run_dir) != before:
                continue
            try:
                write_json(path, {'schema_version': _version, 'source': key,
                                  'revision': before, 'view': view})
            except OSError:
                # A read-only installation can still browse without a disk cache.
                pass
            if len(_memory) >= 1024:
                _memory.clear()
            _memory[key] = (before, view)
            return view
        raise ValueError('Evaluation records changed while loading; retry the request.')
