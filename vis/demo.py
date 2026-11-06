"""Start the bundled, registered small London example without API credentials."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
WORLD_ID='world_demo'


def prepare_demo(root: Path=ROOT) -> Path:
    """Install the packaged registry once; never overwrite an existing world."""
    root=Path(root).resolve()
    bundle=root/'examples/world_studio/registered_world'
    target=root/'outputs/worlds'/WORLD_ID
    manifest=target/'world_manifest.json'
    if target.exists():
        if not manifest.is_file():
            raise ValueError('The example world directory exists but its registry is incomplete. Restore it before starting the example.')
        return manifest
    proof=json.loads((root/'data/manifests/app_demo.json').read_text(encoding='utf-8'))
    for relative,expected in proof['files'].items():
        source=(bundle/relative).resolve()
        if not source.is_relative_to(bundle.resolve()) or not source.is_file():
            raise ValueError('A bundled example asset is missing or outside the example directory.')
        if hashlib.sha256(source.read_bytes()).hexdigest()!=expected:
            raise ValueError('A bundled example asset failed its integrity check: '+relative)
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=Path(tempfile.mkdtemp(prefix='.world_demo_',dir=target.parent))
    try:
        shutil.copytree(bundle,temporary,dirs_exist_ok=True)
        for attempt in range(3):
            try:
                temporary.rename(target)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.1 * (attempt + 1))
    except BaseException:
        if temporary.exists():shutil.rmtree(temporary)
        raise
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8769)
    args=parser.parse_args()
    prepare_demo()
    from vis.tasks.result_examples import install_result_examples
    install_result_examples(ROOT)
    import runpy
    sys.argv=['vis.server','--world-id',WORLD_ID,'--port',str(args.port)]
    runpy.run_module("vis.server", run_name="__main__", alter_sys=True)

if __name__=='__main__':
    main()
