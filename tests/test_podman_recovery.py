"""Real Podman + actual SIGKILL controller recovery in an independent Linux VM."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from tests.snapshot_recovery_probe import config_for

pytestmark = pytest.mark.skipif(
    sys.platform != 'linux' or not os.environ.get('MINDCODE_PODMAN_TEST_IMAGE'),
    reason='requires independent Linux VM and explicit trusted image',
)


@pytest.mark.parametrize('window', ['before_journal', 'partial', 'applied'])
def test_real_killed_publication_resumes_once(tmp_path, window):
    root = tmp_path / 'plain'
    root.mkdir()
    (root / 'seed.txt').write_text('user')
    image = os.environ['MINDCODE_PODMAN_TEST_IMAGE']
    args = [sys.executable, '-m', 'tests.snapshot_recovery_probe', str(root), image]
    child = subprocess.Popen([*args, window], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
    try:
        stdout, stderr = child.communicate(timeout=180)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    assert child.returncode == -9, (stdout, stderr)
    identity = json.loads((tmp_path / 'probe.json').read_text())
    config = config_for(root, image)
    with sqlite3.connect(config.state_root / 'runs.db') as db:
        assert db.execute('SELECT state FROM attempt').fetchone() == ('promoting',)
    staging = list((config.state_root / 'task-staging').glob('*/data/*'))
    assert len(staging) == 1 and list(staging[0].iterdir())
    if window == 'applied':
        (root / 'seed.txt').write_text('editor-after-publish')
    resumed = subprocess.run([*args, 'resume', identity['run_id']], capture_output=True,
                             text=True, timeout=180)
    assert resumed.returncode == 0, resumed.stderr
    result = json.loads(resumed.stdout.strip().splitlines()[-1])
    assert result['accepted'] and result['integrated'] and result['no_workers'], result
    assert (root / 'seed.txt').read_text() == (
        'editor-after-publish' if window == 'applied' else 'userX')
    assert (root / 'note.txt').read_text() == 'verified'
    assert not list((config.state_root / 'task-staging').glob('*/data/*'))
    assert not list(config.state_root.rglob('journal.json'))
    with sqlite3.connect(config.state_root / 'runs.db') as db:
        assert db.execute('SELECT state FROM attempt').fetchone() == ('promoted',)
        assert db.execute('SELECT publication_state FROM snapshot_run').fetchone() == ('applied',)
    repeated = subprocess.run([*args, 'resume', identity['run_id']], capture_output=True,
                              text=True, timeout=180)
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout.strip().splitlines()[-1])['integrated']
    assert (root / 'seed.txt').read_text() == (
        'editor-after-publish' if window == 'applied' else 'userX')
