"""Tests for herdr_undo_close.restore: pane_id stripping before apply,
workspace fallback, index clamping, and the pop-only-after-success /
remove-by-identity semantics of reopen_at. A fake API stands in for the
socket - no network, no real Herdr process involved."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from herdr_undo_close import restore, store


class FakeApi(object):
    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        answer = self.script.get(method, {})
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, type) and issubclass(answer, Exception):
            raise answer("fake error")
        if callable(answer):
            return answer(params)
        return answer


def make_record(tab_id="w1:t7", workspace_id="w1", index=4, closed_ms=1000, with_agent=True):
    root = {
        "type": "split",
        "direction": "right",
        "ratio": 0.5,
        "first": {"type": "pane", "pane_id": "pA", "cwd": "/proj"},
        "second": {"type": "pane", "pane_id": "pB", "cwd": "/proj/sub"},
    }
    panes = [{"pane_id": "pA", "cwd": "/proj"}, {"pane_id": "pB", "cwd": "/proj/sub"}]
    if with_agent:
        panes[0]["agent"] = "claude"
        panes[0]["agent_status"] = "working"
    return {
        "tab_id": tab_id,
        "workspace_id": workspace_id,
        "workspace_label": "maestro",
        "label": "probe-3",
        "index": index,
        "root": root,
        "panes": panes,
        "refreshed_ms": 900,
        "closed_ms": closed_ms,
    }


class TestClamp(unittest.TestCase):
    def test_within_range_is_unchanged(self):
        self.assertEqual(restore.clamp(3, 0, 10), 3)

    def test_clamped_to_upper_bound(self):
        self.assertEqual(restore.clamp(99, 0, 5), 5)

    def test_clamped_to_lower_bound(self):
        self.assertEqual(restore.clamp(-1, 0, 5), 0)

    def test_zero_tabs_clamps_to_zero(self):
        self.assertEqual(restore.clamp(4, 0, 0), 0)


class TestBuildApplyParams(unittest.TestCase):
    def test_pane_id_stripped_from_root(self):
        rec = make_record()
        params = restore.build_apply_params(rec, "w1", focus=True)
        self.assertNotIn("pane_id", params["root"]["first"])
        self.assertNotIn("pane_id", params["root"]["second"])

    def test_tab_id_never_included(self):
        rec = make_record()
        params = restore.build_apply_params(rec, "w1", focus=True)
        self.assertNotIn("tab_id", params)

    def test_carries_label_workspace_and_focus(self):
        rec = make_record()
        params = restore.build_apply_params(rec, "w9", focus=False)
        self.assertEqual(params["workspace_id"], "w9")
        self.assertEqual(params["tab_label"], "probe-3")
        self.assertEqual(params["focus"], False)


class TestChooseWorkspace(unittest.TestCase):
    def test_recorded_workspace_still_live(self):
        rec = make_record(workspace_id="w1")
        workspaces = [{"workspace_id": "w1", "label": "maestro"}, {"workspace_id": "w2", "label": "other"}]
        workspace_id, fallback = restore.choose_workspace(rec, workspaces, env_workspace_id=None)
        self.assertEqual(workspace_id, "w1")
        self.assertFalse(fallback)

    def test_falls_back_to_env_workspace_when_recorded_is_gone(self):
        rec = make_record(workspace_id="w1")
        workspaces = [{"workspace_id": "w2", "label": "other"}]
        workspace_id, fallback = restore.choose_workspace(rec, workspaces, env_workspace_id="w2")
        self.assertEqual(workspace_id, "w2")
        self.assertTrue(fallback)

    def test_falls_back_to_focused_workspace_when_no_env(self):
        rec = make_record(workspace_id="w1")
        workspaces = [{"workspace_id": "w2", "label": "a"}, {"workspace_id": "w3", "label": "b", "focused": True}]
        workspace_id, fallback = restore.choose_workspace(rec, workspaces, env_workspace_id=None)
        self.assertEqual(workspace_id, "w3")
        self.assertTrue(fallback)

    def test_falls_back_to_first_workspace_as_last_resort(self):
        rec = make_record(workspace_id="w1")
        workspaces = [{"workspace_id": "w2", "label": "a"}]
        workspace_id, fallback = restore.choose_workspace(rec, workspaces, env_workspace_id=None)
        self.assertEqual(workspace_id, "w2")
        self.assertTrue(fallback)

    def test_raises_when_no_workspace_exists_at_all(self):
        rec = make_record(workspace_id="w1")
        with self.assertRaises(ValueError):
            restore.choose_workspace(rec, [], env_workspace_id=None)


class TestSummarizeAgents(unittest.TestCase):
    def test_lists_distinct_agent_kinds(self):
        rec = make_record(with_agent=True)
        self.assertEqual(restore.summarize_agents(rec), ["claude"])

    def test_empty_when_no_agents(self):
        rec = make_record(with_agent=False)
        self.assertEqual(restore.summarize_agents(rec), [])


class TestRestoreRecord(unittest.TestCase):
    def test_position_restored_when_tab_move_succeeds(self):
        rec = make_record(index=4)
        api = FakeApi(
            {
                "layout.apply": {"layout": {"tab_id": "w1:new"}},
                "tab.list": {"tabs": [{"workspace_id": "w1"}] * 10},
                "tab.move": {},
            }
        )
        result = restore.restore_record(api, rec, [{"workspace_id": "w1"}], None, focus=True)
        self.assertEqual(result["new_tab_id"], "w1:new")
        self.assertTrue(result["position_restored"])
        move_calls = [p for (m, p) in api.calls if m == "tab.move"]
        self.assertEqual(move_calls[0]["insert_index"], 4)

    def test_insert_index_clamped_to_available_tabs(self):
        rec = make_record(index=99)
        api = FakeApi(
            {
                "layout.apply": {"layout": {"tab_id": "w1:new"}},
                "tab.list": {"tabs": [{"workspace_id": "w1"}] * 3},
                "tab.move": {},
            }
        )
        restore.restore_record(api, rec, [{"workspace_id": "w1"}], None, focus=True)
        move_calls = [p for (m, p) in api.calls if m == "tab.move"]
        self.assertEqual(move_calls[0]["insert_index"], 2)

    def test_tab_move_failure_is_not_fatal(self):
        rec = make_record()
        api = FakeApi(
            {
                "layout.apply": {"layout": {"tab_id": "w1:new"}},
                "tab.list": {"tabs": [{"workspace_id": "w1"}]},
                "tab.move": RuntimeError("nope"),
            }
        )
        result = restore.restore_record(api, rec, [{"workspace_id": "w1"}], None, focus=True)
        self.assertEqual(result["new_tab_id"], "w1:new")
        self.assertFalse(result["position_restored"])

    def test_layout_apply_failure_propagates(self):
        rec = make_record()
        api = FakeApi({"layout.apply": RuntimeError("apply failed")})
        with self.assertRaises(RuntimeError):
            restore.restore_record(api, rec, [{"workspace_id": "w1"}], None, focus=True)

    def test_missing_tab_id_in_result_raises(self):
        rec = make_record()
        api = FakeApi({"layout.apply": {"layout": {}}})
        with self.assertRaises(ValueError):
            restore.restore_record(api, rec, [{"workspace_id": "w1"}], None, focus=True)


class TestReopenAt(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix="herdr-undo-close-test-")
        self.config = {"max_entries": 25, "focus": True, "notify": True}

    def tearDown(self):
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def test_nothing_to_reopen_returns_none(self):
        store.save_state(self.state_dir, store.default_state())
        api = FakeApi({"workspace.list": {"workspaces": [{"workspace_id": "w1"}]}})
        result = restore.reopen_at(api, self.state_dir, self.config, None)
        self.assertIsNone(result)

    def test_successful_restore_removes_only_the_matching_entry(self):
        rec_a = make_record(tab_id="w1:tA", closed_ms=2000)
        rec_b = make_record(tab_id="w1:tB", closed_ms=1000)
        store.save_state(self.state_dir, {"version": 1, "index": {}, "closed": [rec_a, rec_b]})

        api = FakeApi(
            {
                "workspace.list": {"workspaces": [{"workspace_id": "w1"}]},
                "layout.apply": {"layout": {"tab_id": "w1:new"}},
                "tab.list": {"tabs": [{"workspace_id": "w1"}]},
                "tab.move": {},
            }
        )
        result = restore.reopen_at(api, self.state_dir, self.config, None, index=0)
        self.assertIsNotNone(result)
        self.assertEqual(result["new_tab_id"], "w1:new")

        saved = store.load_state(self.state_dir)
        self.assertEqual([c["tab_id"] for c in saved["closed"]], ["w1:tB"])

    def test_failed_restore_leaves_closed_stack_untouched(self):
        rec_a = make_record(tab_id="w1:tA", closed_ms=2000)
        store.save_state(self.state_dir, {"version": 1, "index": {}, "closed": [rec_a]})

        api = FakeApi(
            {
                "workspace.list": {"workspaces": [{"workspace_id": "w1"}]},
                "layout.apply": RuntimeError("boom"),
            }
        )
        with self.assertRaises(RuntimeError):
            restore.reopen_at(api, self.state_dir, self.config, None, index=0)

        saved = store.load_state(self.state_dir)
        self.assertEqual([c["tab_id"] for c in saved["closed"]], ["w1:tA"])

    def test_removal_is_by_identity_not_position(self):
        # Simulate a concurrent record pass pushing a new death onto the
        # front of `closed` while this restore's socket round trip is in
        # flight. Removal must still target the original (tab_id,
        # closed_ms), not "whatever is now at index 0".
        rec_a = make_record(tab_id="w1:tA", closed_ms=2000)
        store.save_state(self.state_dir, {"version": 1, "index": {}, "closed": [rec_a]})

        inserted = {"done": False}

        def fake_layout_apply(params):
            if not inserted["done"]:
                state = store.load_state(self.state_dir)
                intruder = make_record(tab_id="w1:tC", closed_ms=3000)
                state["closed"].insert(0, intruder)
                store.save_state(self.state_dir, state)
                inserted["done"] = True
            return {"layout": {"tab_id": "w1:new"}}

        api = FakeApi(
            {
                "workspace.list": {"workspaces": [{"workspace_id": "w1"}]},
                "layout.apply": fake_layout_apply,
                "tab.list": {"tabs": [{"workspace_id": "w1"}]},
                "tab.move": {},
            }
        )
        restore.reopen_at(api, self.state_dir, self.config, None, index=0)

        saved = store.load_state(self.state_dir)
        remaining_ids = [c["tab_id"] for c in saved["closed"]]
        self.assertIn("w1:tC", remaining_ids)
        self.assertNotIn("w1:tA", remaining_ids)


if __name__ == "__main__":
    unittest.main()
