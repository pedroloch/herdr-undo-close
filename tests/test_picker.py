"""Tests for herdr_undo_close.picker: _read_key's EOF handling, and the
picker's key loop treating EOF as a cancel rather than crashing."""

import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from herdr_undo_close import picker, store


class _DummyTermios(object):
    TCSADRAIN = 0

    @staticmethod
    def tcgetattr(fd):
        return None

    @staticmethod
    def tcsetattr(fd, when, attrs):
        pass


class _DummyTty(object):
    @staticmethod
    def setraw(fd):
        pass


def _closed_record():
    return {
        "tab_id": "w1:t1",
        "workspace_id": "w1",
        "workspace_label": "maestro",
        "label": "probe",
        "index": 0,
        "root": {"type": "pane", "cwd": "/x"},
        "panes": [{"pane_id": "pA", "cwd": "/x"}],
        "closed_ms": 1,
    }


class TestReadKeyEof(unittest.TestCase):
    def test_returns_eof_sentinel_when_read_yields_empty_bytes(self):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)  # closed write end: os.read(read_fd, 1) now yields b""
        try:
            self.assertEqual(picker._read_key(read_fd), "eof")
        finally:
            os.close(read_fd)


class TestRunPickerCancelsOnEof(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix="herdr-undo-close-test-")
        store.save_state(self.state_dir, {"version": 1, "index": {}, "closed": [_closed_record()]})

    def tearDown(self):
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def test_eof_during_key_loop_cancels_instead_of_raising(self):
        calls = {"n": 0}

        def fake_read(fd, n):
            calls["n"] += 1
            if calls["n"] > 3:
                raise AssertionError("key loop did not cancel on EOF within 3 reads")
            return b""

        with mock.patch.object(picker, "termios", _DummyTermios), \
                mock.patch.object(picker, "tty", _DummyTty), \
                mock.patch("os.read", side_effect=fake_read), \
                mock.patch("sys.stdout", new=io.StringIO()), \
                mock.patch("sys.stdin") as fake_stdin:
            fake_stdin.isatty.return_value = True
            fake_stdin.fileno.return_value = 0
            result = picker.run_picker(self.state_dir, api=None, config={}, env_workspace_id=None)

        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
