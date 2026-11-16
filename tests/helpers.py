"""Shared test helpers: throwaway project roots."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from creek import paths


class ProjectCase(unittest.TestCase):
    """Each test gets a fresh git-like project root with an empty .creek dir."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="creek-test-"))
        (self.tmp / ".git").mkdir()
        self._old_env = {k: os.environ.get(k) for k in ("CREEK_ROOT", "CLAUDE_PROJECT_DIR", "CREEK_BRANCH", "CREEK_NODE", "ORCA_CHAT_SCRATCH_DIR")}
        os.environ["CREEK_ROOT"] = str(self.tmp)
        for k in ("CLAUDE_PROJECT_DIR", "CREEK_BRANCH", "CREEK_NODE", "ORCA_CHAT_SCRATCH_DIR"):
            os.environ.pop(k, None)
        self.cdir = paths.creek_dir(self.tmp)

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_events(self, events):
        with open(paths.events_path(self.cdir), "a") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")


def comparable(state: dict) -> dict:
    s = json.loads(json.dumps(state))
    s.pop("bad_lines", None)
    return s
