"""Bounded crash injection into test-owned, credential-free subprocesses only."""

import os
from pathlib import Path
import subprocess
import sys
import time


def kill_at_marker(script, data_dir, marker, *arguments):
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(data_dir), str(marker), *arguments],
        cwd=marker.parent,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).parents[1])},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), (
            child.communicate(timeout=1) if child.poll() is not None else "child did not reach checkpoint"
        )
        assert child.poll() is None
        child.kill()
        child.communicate(timeout=5)
        assert child.returncode != 0
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
