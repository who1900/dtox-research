"""Populate actual FTS ownership while existing corpus writers are idle."""
import fcntl
import json
from pathlib import Path
import subprocess
import time

from ops.reconcile_fts_catalog import run
from ops.repair_corpus_labels import _atomic_json


def main():
    base = Path('/opt/dtox-corpus-quality-20261013')
    data = Path('/opt/dtox-research')
    started = time.time()
    with (base/'labels-worker.lock').open('a') as labels, (base/'points-worker.lock').open('a') as points:
        for lock in (labels, points):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            for unit in ('dtox-research.service', 'dtox-pipeline.service'):
                result = subprocess.run(['systemctl', 'show', unit, '--value', '--property=ActiveState'],
                                        capture_output=True, text=True, check=True)
                if result.stdout.strip() not in ('inactive', 'failed'):
                    raise RuntimeError('pipeline writer is active')
            result = run((data/'fts.db').resolve(), base/'fts-rowmap.sqlite',
                         max_seconds=60, writers_frozen=True)
            result['elapsed_seconds'] = round(time.time()-started, 3)
            _atomic_json(base/'catalog-worker-status.json', result)
            print(json.dumps(result), flush=True)
            if result['phase'] == 'complete':
                break


if __name__ == '__main__':
    main()
