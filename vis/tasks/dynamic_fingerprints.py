"""Process-local file fingerprints for read-only Dynamic confirmations."""
import hashlib
from pathlib import Path
from threading import RLock

_hashes = {}
_lock = RLock()


def stamp(path):
    try:
        s = Path(path).stat()
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    except FileNotFoundError:
        return None


def fingerprint(path):
    path = Path(path).resolve()
    before = stamp(path)
    if before is None:
        return None
    with _lock:
        cached = _hashes.get(str(path))
        if cached and cached[0] == before:
            return cached[1]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if before != stamp(path):
        raise ValueError('文件在校验期间发生变化，请重试')
    with _lock:
        if len(_hashes) >= 10000:
            _hashes.clear()
        _hashes[str(path)] = (before, digest)
    return digest


def source_stamps(root, world, cities, kinds, output_mode):
    """Include membership, review and batch metadata, not just eligible tasks."""
    paths = set()
    for city in cities:
        for kind in [*kinds, *(['dynamic_replanning'] if output_mode == 'append' else [])]:
            scope = Path(root) / kind / world / city['city_id']
            paths.add(scope)
            if scope.is_dir():
                for batch in scope.iterdir():
                    if batch.is_dir():
                        paths.add(batch)
                        paths.update(batch.glob('*.json'))
                        paths.add(batch / 'tasks')
                        paths.update(batch.glob('tasks/*/*.json'))
    return tuple(sorted((str(p.resolve()), stamp(p)) for p in paths))
