"""Read the generation verdict for display, without rerunning construction."""
from pathlib import Path
from shared.io import read_json
from .dynamic_fingerprints import stamp


def browse_revision(root, world, cities, kinds):
    """Fingerprint only files consumed by task-list discovery, not episode data."""
    paths = set()
    for city in cities:
        for kind in kinds:
            scope = Path(root) / kind / world / city['city_id']
            paths.add(scope)
            if not scope.is_dir():
                continue
            for batch in scope.iterdir():
                if not batch.is_dir() or batch.name == 'latest' or batch.name.startswith('_'):
                    continue
                paths.add(batch)
                paths.update(batch.glob('*.json'))
                tasks = batch / 'tasks'
                paths.add(tasks)
                if tasks.is_dir():
                    for task in tasks.iterdir():
                        if task.is_dir() and not task.name.startswith('_'):
                            paths.update((task / 'task.json', task / 'task_validation.json'))
    return tuple(sorted((str(path.resolve()), stamp(path)) for path in paths))


def recorded_formal_task(task_dir):
    task_dir = Path(task_dir)
    try:
        batch_path = task_dir.parent.parent / 'batch_metadata.json'
        if batch_path.is_file() and read_json(batch_path).get('evaluation_eligibility') == 'ineligible':
            return False
        record = read_json(task_dir / 'task_validation.json')
        return record.get('state') == 'formal' and record.get('valid') is True
    except (OSError, ValueError, AttributeError):
        return False
