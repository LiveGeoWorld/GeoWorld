"""Install the small recorded result excerpts once, in the normal result layout."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time

def install_result_examples(root):
    root=Path(root).resolve()
    bundle=root/'examples/test_results/evaluations'
    manifest=json.loads((root/'data/manifests/result_samples.json').read_text(encoding='utf-8'))
    for relative,expected in manifest['files'].items():
        path=(bundle/relative).resolve()
        if not path.is_relative_to(bundle.resolve()) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError('A bundled evaluation example failed its integrity check: '+relative)
    for sample in manifest['samples']:
        relative=Path(sample['destination']).relative_to('examples/test_results/evaluations')
        source=bundle/relative
        target=root/'outputs/evaluations'/relative
        if target.exists():continue
        target.parent.mkdir(parents=True,exist_ok=True)
        temporary=Path(tempfile.mkdtemp(prefix='.example_',dir=target.parent))
        try:
            shutil.copytree(source,temporary,dirs_exist_ok=True)
            for attempt in range(3):
                try:
                    temporary.rename(target)
                    break
                except PermissionError:
                    if attempt==2:raise
                    time.sleep(0.1*(attempt+1))
        except BaseException:
            if temporary.exists():shutil.rmtree(temporary)
            raise
    return len(manifest['samples'])
