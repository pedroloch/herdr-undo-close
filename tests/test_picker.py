"""Tests for herdr_undo_close.picker: row rendering for both kinds of closed
entry, _read_key's EOF handling, and the picker's key loop treating EOF as a
cancel rather than crashing."""

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


def _closed_pane_record(label="build", cwd="/home/pedro/proj"):
    return {
        "kind": "pane",
        "pane_id": "w1:p4",
        "terminal_id": "term_a",
        "tab_id": "w1:t1",
        "workspace_id": "w1",
        "workspace_label": "maestro",
        "tab_label": "probe",
        "cwd": cwd,
        "label": label,
        "parent": {
            "direction": "down",
            "ratio": 0.7,
            "dead_side": "second",
            "sibling_pane_ids": ["pB"],
            "sibling_is_leaf": True,
        },
        "agent": "claude",
        "closed_ms": 1,
    }


class TestPaneName(unittest.TestCase):
    def test_label_wins(self):
        self.assertEqual(picker.pane_name("build", "/home/pedro/proj"), "build")

    def test_falls_back_to_the_cwd_basename(self):
        self.assertEqual(picker.pane_name(None, "/home/pedro/proj/"), "proj")

    def test_falls_back_to_the_default_when_there_is_nothing(self):
        self.assertEqual(picker.pane_name(None, None), "pane")


class TestRowRendering(unittest.TestCase):
    def test_v1_kindless_entry_renders_as_a_tab(self):
        self.assertEqual(picker.row_kind(_closed_record()), "tab")
        self.assertEqual(picker.row_title(_closed_record()), "probe")
        self.assertIn("panes:1", picker.row_detail(_closed_record()))

    def test_pane_entry_shows_its_own_name_and_its_tab(self):
        rec = _closed_pane_record()
        self.assertEqual(picker.row_kind(rec), "pane")
        self.assertEqual(picker.row_title(rec), "build in probe")

    def test_unlabelled_pane_falls_back_to_the_cwd_basename(self):
        rec = _closed_pane_record(label=None)
        self.assertEqual(picker.row_title(rec), "proj in probe")

    def test_pane_detail_shows_the_split_direction_and_the_agent(self):
        detail = picker.row_detail(_closed_pane_record())
        self.assertIn("split:down", detail)
        self.assertIn("agent:claude", detail)

    def test_rows_fit_in_eighty_columns(self):
        five_days = 5 * 24 * 60 * 60 * 1000
        for rec in (_closed_record(), _closed_pane_record()):
            line = picker.row_label(0, rec, now_ms=rec["closed_ms"] + five_days)
            self.assertLessEqual(len(line) + 2, 80, line)  # +2 for the "> " marker

    def test_malformed_pane_entry_still_renders(self):
        rec = {"kind": "pane", "closed_ms": 1}
        self.assertEqual(picker.row_title(rec), "pane in ?")
        self.assertIn("?", picker.row_detail(rec))

    def test_render_lines_marks_the_selection_and_mentions_both_kinds(self):
        lines = picker.render_lines([_closed_record(), _closed_pane_record()], 1, now_ms=1000)
        self.assertIn("tab or pane", lines[0])
        self.assertTrue(lines[2].startswith("  1."))
        self.assertTrue(lines[3].startswith("> 2."))


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
