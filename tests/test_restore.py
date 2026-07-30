"""Tests for herdr_undo_close.restore: pane_id stripping before apply,
workspace fallback, index clamping, relaunching what a pane was running, and
the pop-only-after-success / remove-by-identity semantics of reopen_at. A
fake API stands in for the socket - no network, no real Herdr process
involved."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from herdr_undo_close import relaunch, restore, store

# An explicit allowlist rather than the shipped default, so these tests keep
# testing the mechanism when the default list changes.
CONFIG = {
    "max_entries": 25,
    "focus": True,
    "notify": True,
    "undo_panes": True,
    "restore_process": True,
    "relaunch_agents": True,
    "relaunch_programs": ["nvim", "less"],
}


def config(**overrides):
    result = dict(CONFIG)
    result.update(overrides)
    return result


def ready_shell(pane_id="w1:p9"):
    """A pane.process_info envelope for a shell sitting at its prompt - the
    real socket shape, payload nested under "process_info" and not flat."""
    return {
        "type": "pane_process_info",
        "process_info": {
            "pane_id": pane_id,
            "shell_pid": 100,
            "foreground_processes": [{"pid": 100, "name": "-zsh", "argv": ["-zsh"]}],
        },
    }


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


def make_record(
    tab_id="w1:t7",
    workspace_id="w1",
    index=4,
    closed_ms=1000,
    with_agent=True,
    relaunches=None,
):
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
    # Positionally aligned with `panes`, the way a real record is.
    for pane, intent in zip(panes, relaunches or []):
        if intent:
            pane["relaunch"] = intent
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


def make_pane_record(
    pane_id="pA",
    tab_id="w1:t7",
    dead_side="first",
    sibling_pane_ids=None,
    sibling_is_leaf=True,
    ratio=0.7,
    direction="right",
    closed_ms=2000,
    label="build",
    command=None,
    agent=None,
    relaunch_intent=None,
):
    entry = {
        "kind": "pane",
        "pane_id": pane_id,
        "terminal_id": "term_" + pane_id,
        "tab_id": tab_id,
        "workspace_id": "w1",
        "workspace_label": "maestro",
        "tab_label": "probe-3",
        "cwd": "/proj",
        "label": label,
        "env": {"X": "1"},
        "command": command,
        "parent": {
            "direction": direction,
            "ratio": ratio,
            "dead_side": dead_side,
            "sibling_pane_ids": ["pB"] if sibling_pane_ids is None else sibling_pane_ids,
            "sibling_is_leaf": sibling_is_leaf,
        },
        "closed_ms": closed_ms,
    }
    if agent:
        entry["agent"] = agent
    if relaunch_intent:
        entry["relaunch"] = relaunch_intent
    return entry


def pane_api(live_tab_ids=("w1:t7",), live_pane_ids=("pB",), new_pane_id="w1:p9", extra=None):
    """A fake api for the pane-restore path: a tab.list, a layout.export whose
    tree holds live_pane_ids in order, and a pane.split that hands back an id."""
    root = None
    for pane_id in reversed(list(live_pane_ids)):
        leaf = {"type": "pane", "pane_id": pane_id}
        root = leaf if root is None else {
            "type": "split",
            "direction": "right",
            "ratio": 0.5,
            "first": leaf,
            "second": root,
        }
    script = {
        "tab.list": {"tabs": [{"tab_id": t, "workspace_id": "w1"} for t in live_tab_ids]},
        "layout.export": {"layout": {"root": root}},
        "pane.split": {"pane_id": new_pane_id},
        "pane.rename": {},
        "pane.swap": {},
    }
    if extra:
        script.update(extra)
    return FakeApi(script)


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
        params = restore.build_apply_params(rec, "w1", focus=True, config=CONFIG)
        self.assertNotIn("pane_id", params["root"]["first"])
        self.assertNotIn("pane_id", params["root"]["second"])

    def test_tab_id_never_included(self):
        rec = make_record()
        params = restore.build_apply_params(rec, "w1", focus=True, config=CONFIG)
        self.assertNotIn("tab_id", params)

    def test_carries_label_workspace_and_focus(self):
        rec = make_record()
        params = restore.build_apply_params(rec, "w9", focus=False, config=CONFIG)
        self.assertEqual(params["workspace_id"], "w9")
        self.assertEqual(params["tab_label"], "probe-3")
        self.assertEqual(params["focus"], False)

    def test_no_relaunch_intent_means_no_command_on_any_node(self):
        rec = make_record(with_agent=False)
        params = restore.build_apply_params(rec, "w1", focus=True, config=CONFIG)
        self.assertNotIn("command", params["root"]["first"])
        self.assertNotIn("command", params["root"]["second"])


def three_pane_record(relaunches):
    """A tab of three panes, pA | (pB / pC), with one relaunch slot each."""
    rec = make_record(with_agent=False)
    rec["root"] = {
        "type": "split",
        "direction": "right",
        "ratio": 0.5,
        "first": {"type": "pane", "cwd": "/a"},
        "second": {
            "type": "split",
            "direction": "down",
            "ratio": 0.5,
            "first": {"type": "pane", "cwd": "/b"},
            "second": {"type": "pane", "cwd": "/c"},
        },
    }
    rec["panes"] = [{"cwd": "/a"}, {"cwd": "/b"}, {"cwd": "/c"}]
    for pane, intent in zip(rec["panes"], relaunches):
        if intent:
            pane["relaunch"] = intent
    return rec


class TestTabRelaunchInjection(unittest.TestCase):
    """layout.apply honours a `command` on a pane node, so a tab restore
    brings a process back by hanging one there. The pairing between the
    record's `panes` list and the tree's pane nodes is positional, so the
    interesting failure is a command landing on the wrong pane."""

    def _nodes(self, params):
        return [
            params["root"]["first"],
            params["root"]["second"]["first"],
            params["root"]["second"]["second"],
        ]

    def test_command_lands_only_on_the_pane_that_had_an_intent(self):
        rec = three_pane_record(
            [None, {"kind": "process", "argv": ["nvim", "README.md"]}, None]
        )
        params = restore.build_apply_params(rec, "w1", focus=True, config=CONFIG)
        first, second, third = self._nodes(params)
        self.assertEqual(second["command"], ["nvim", "README.md"])
        self.assertNotIn("command", first)
        self.assertNotIn("command", third)
        # The zip must not have shuffled anything else either.
        self.assertEqual([n["cwd"] for n in self._nodes(params)], ["/a", "/b", "/c"])

    def test_flatten_order_is_the_order_the_zip_relies_on(self):
        rec = three_pane_record([None, None, None])
        params = restore.build_apply_params(rec, "w1", focus=True, config=CONFIG)
        from herdr_undo_close import record as record_mod

        self.assertEqual(
            [n.get("cwd") for n in record_mod.flatten_pane_nodes(params["root"])],
            [p["cwd"] for p in rec["panes"]],
        )

    def test_agent_pane_gets_a_resume_command(self):
        rec = three_pane_record(
            [{"kind": "agent", "agent": "claude", "session_id": "sess-1"}, None, None]
        )
        params = restore.build_apply_params(rec, "w1", focus=True, config=CONFIG)
        self.assertEqual(self._nodes(params)[0]["command"], ["claude", "-r", "sess-1"])

    def test_a_program_outside_the_allowlist_is_never_injected(self):
        rec = three_pane_record([{"kind": "process", "argv": ["psql", "prod"]}, None, None])
        params = restore.build_apply_params(rec, "w1", focus=True, config=CONFIG)
        self.assertNotIn("command", self._nodes(params)[0])

    def test_restore_process_false_injects_nothing(self):
        rec = three_pane_record(
            [{"kind": "process", "argv": ["nvim", "README.md"]}, None, None]
        )
        params = restore.build_apply_params(
            rec, "w1", focus=True, config=config(restore_process=False)
        )
        self.assertNotIn("command", self._nodes(params)[0])

    def test_relaunch_agents_false_injects_nothing_for_an_agent(self):
        rec = three_pane_record(
            [{"kind": "agent", "agent": "claude", "session_id": None}, None, None]
        )
        params = restore.build_apply_params(
            rec, "w1", focus=True, config=config(relaunch_agents=False)
        )
        self.assertNotIn("command", self._nodes(params)[0])


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

    def test_a_resumed_agent_is_not_also_reported_as_not_restarted(self):
        rec = make_record(with_agent=True)
        plans = [{"kind": "agent", "program": "claude", "allowed": True}, None]
        self.assertEqual(restore.summarize_agents(rec, plans), [])

    def test_an_agent_that_was_not_resumed_is_still_reported(self):
        rec = make_record(with_agent=True)
        plans = [{"kind": "agent", "program": "claude", "allowed": False}, None]
        self.assertEqual(restore.summarize_agents(rec, plans), ["claude"])


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
        result = restore.restore_record(api, rec, [{"workspace_id": "w1"}], None, focus=True, config=CONFIG)
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
        restore.restore_record(api, rec, [{"workspace_id": "w1"}], None, focus=True, config=CONFIG)
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
        result = restore.restore_record(api, rec, [{"workspace_id": "w1"}], None, focus=True, config=CONFIG)
        self.assertEqual(result["new_tab_id"], "w1:new")
        self.assertFalse(result["position_restored"])

    def test_layout_apply_failure_propagates(self):
        rec = make_record()
        api = FakeApi({"layout.apply": RuntimeError("apply failed")})
        with self.assertRaises(RuntimeError):
            restore.restore_record(api, rec, [{"workspace_id": "w1"}], None, focus=True, config=CONFIG)

    def test_missing_tab_id_in_result_raises(self):
        rec = make_record()
        api = FakeApi({"layout.apply": {"layout": {}}})
        with self.assertRaises(ValueError):
            restore.restore_record(api, rec, [{"workspace_id": "w1"}], None, focus=True, config=CONFIG)

    def test_relaunch_outcomes_are_reported_and_the_agent_caveat_drops(self):
        rec = make_record(
            relaunches=[
                {"kind": "agent", "agent": "claude", "session_id": "sess-1"},
                {"kind": "process", "argv": ["psql", "prod"]},
            ]
        )
        api = FakeApi(
            {
                "layout.apply": {"layout": {"tab_id": "w1:new"}},
                "tab.list": {"tabs": [{"workspace_id": "w1"}]},
                "tab.move": {},
            }
        )
        result = restore.restore_record(
            api, rec, [{"workspace_id": "w1"}], None, focus=True, config=CONFIG
        )
        self.assertEqual([p["program"] for p in result["relaunched"]], ["claude"])
        self.assertEqual([p["program"] for p in result["not_relaunched"]], ["psql"])
        self.assertEqual(result["relaunch_failed"], [])
        # claude is running again, so "claude was not restarted" would be a lie.
        self.assertEqual(result["agent_kinds"], [])


class TestChooseAnchor(unittest.TestCase):
    def test_first_recorded_id_wins_when_it_is_alive(self):
        anchor, is_recorded = restore.choose_anchor(["pB", "pC"], ["pC", "pB"])
        self.assertEqual(anchor, "pB")
        self.assertTrue(is_recorded)

    def test_next_recorded_id_is_used_when_the_adjacent_one_died(self):
        anchor, is_recorded = restore.choose_anchor(["pB", "pC"], ["pC"])
        self.assertEqual(anchor, "pC")
        self.assertFalse(is_recorded)

    def test_falls_back_to_any_live_pane_of_the_tab(self):
        anchor, is_recorded = restore.choose_anchor(["pB"], ["pZ"])
        self.assertEqual(anchor, "pZ")
        self.assertFalse(is_recorded)

    def test_no_live_pane_yields_no_anchor(self):
        self.assertEqual(restore.choose_anchor(["pB"], []), (None, False))


class TestRestorePaneRecord(unittest.TestCase):
    def setUp(self):
        self.config = {"max_entries": 25, "focus": True, "notify": True, "undo_panes": True}

    def _methods(self, api):
        return [m for (m, _) in api.calls]

    def _params(self, api, method):
        return [p for (m, p) in api.calls if m == method]

    def test_first_side_death_splits_then_swaps(self):
        api = pane_api()
        result = restore.restore_pane_record(api, make_pane_record(dead_side="first"), self.config)

        self.assertEqual(
            self._methods(api),
            ["tab.list", "layout.export", "pane.split", "pane.rename", "pane.swap"],
        )
        split = self._params(api, "pane.split")[0]
        self.assertEqual(split["target_pane_id"], "pB")
        self.assertEqual(split["direction"], "right")
        self.assertEqual(split["ratio"], 0.7)
        self.assertEqual(split["cwd"], "/proj")
        self.assertEqual(split["env"], {"X": "1"})
        self.assertEqual(split["focus"], True)
        self.assertNotIn("command", split)
        # The new pane comes back as `second`, so it swaps INTO the anchor's
        # place: source is the new pane, target is the anchor.
        self.assertEqual(
            self._params(api, "pane.swap")[0],
            {"source_pane_id": "w1:p9", "target_pane_id": "pB"},
        )
        self.assertEqual(self._params(api, "pane.rename")[0], {"pane_id": "w1:p9", "label": "build"})
        self.assertEqual(result["kind"], "pane")
        self.assertEqual(result["new_pane_id"], "w1:p9")
        self.assertTrue(result["exact"])

    def test_second_side_death_does_not_swap_and_keeps_the_ratio_verbatim(self):
        api = pane_api()
        result = restore.restore_pane_record(
            api, make_pane_record(dead_side="second", ratio=0.35), self.config
        )
        self.assertNotIn("pane.swap", self._methods(api))
        self.assertEqual(self._params(api, "pane.split")[0]["ratio"], 0.35)
        self.assertTrue(result["exact"])

    def test_down_direction_is_passed_through(self):
        api = pane_api()
        restore.restore_pane_record(
            api, make_pane_record(direction="down", dead_side="second"), self.config
        )
        self.assertEqual(self._params(api, "pane.split")[0]["direction"], "down")

    def test_unlabelled_pane_is_not_renamed(self):
        api = pane_api()
        restore.restore_pane_record(api, make_pane_record(label=None, dead_side="second"), self.config)
        self.assertNotIn("pane.rename", self._methods(api))

    def test_anchor_fallback_when_the_recorded_sibling_is_dead_is_not_exact(self):
        api = pane_api(live_pane_ids=("pZ",))
        result = restore.restore_pane_record(api, make_pane_record(), self.config)
        self.assertEqual(self._params(api, "pane.split")[0]["target_pane_id"], "pZ")
        self.assertFalse(result["exact"])

    def test_subtree_sibling_is_never_exact_even_when_the_anchor_is_recorded(self):
        api = pane_api(live_pane_ids=("pB", "pC"))
        result = restore.restore_pane_record(
            api,
            make_pane_record(sibling_pane_ids=["pB", "pC"], sibling_is_leaf=False),
            self.config,
        )
        self.assertEqual(self._params(api, "pane.split")[0]["target_pane_id"], "pB")
        self.assertFalse(result["exact"])

    def test_tab_gone_raises_pane_tab_gone(self):
        api = pane_api(live_tab_ids=("w1:t9",))
        with self.assertRaises(restore.PaneTabGone):
            restore.restore_pane_record(api, make_pane_record(), self.config)
        self.assertEqual(self._methods(api), ["tab.list"])

    def test_tab_with_no_panes_raises_value_error(self):
        api = pane_api(live_pane_ids=())
        with self.assertRaises(ValueError):
            restore.restore_pane_record(api, make_pane_record(), self.config)

    def test_split_without_a_pane_id_raises(self):
        api = pane_api(extra={"pane.split": {}})
        with self.assertRaises(ValueError):
            restore.restore_pane_record(api, make_pane_record(), self.config)

    def test_lossy_parts_are_reported(self):
        api = pane_api()
        result = restore.restore_pane_record(
            api,
            make_pane_record(dead_side="second", command=["npm", "run", "dev"], agent="claude"),
            self.config,
        )
        self.assertTrue(result["command_lost"])
        self.assertEqual(result["agent"], "claude")
        self.assertEqual(result["tab_label"], "probe-3")

    def test_failed_swap_leaves_the_pane_but_downgrades_exact(self):
        api = pane_api(extra={"pane.swap": RuntimeError("nope")})
        result = restore.restore_pane_record(api, make_pane_record(dead_side="first"), self.config)
        self.assertEqual(result["new_pane_id"], "w1:p9")
        self.assertFalse(result["exact"])

    def test_failed_rename_is_not_fatal(self):
        api = pane_api(extra={"pane.rename": RuntimeError("nope")})
        result = restore.restore_pane_record(api, make_pane_record(dead_side="second"), self.config)
        self.assertEqual(result["new_pane_id"], "w1:p9")

    def test_layout_apply_is_never_used_on_a_surviving_tab(self):
        # layout.apply with a tab_id is destructive: it closes the tab, mints
        # new pane ids and loses scrollback. The pane path must not call it.
        api = pane_api()
        restore.restore_pane_record(api, make_pane_record(), self.config)
        self.assertNotIn("layout.apply", self._methods(api))


class TestRestorePaneRelaunch(unittest.TestCase):
    """pane.split has no `command`, so the pane path types the command into
    the new pane instead - after proving the shell is ready, never after a
    fixed sleep."""

    def setUp(self):
        self.config = dict(CONFIG)
        # Keep a timeout fast in the tests that deliberately never get ready.
        patcher = mock.patch.object(relaunch, "READY_INTERVAL_S", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _params(self, api, method):
        return [p for (m, p) in api.calls if m == method]

    def _api(self, ready=True, **kwargs):
        extra = {"pane.process_info": ready_shell() if ready else {}, "pane.send_text": {}}
        extra.update(kwargs.pop("extra", None) or {})
        return pane_api(extra=extra, **kwargs)

    def test_allowed_process_is_typed_in_after_the_shell_is_ready(self):
        api = self._api()
        record = make_pane_record(
            dead_side="second",
            relaunch_intent={"kind": "process", "argv": ["nvim", "README.md"]},
        )
        result = restore.restore_pane_record(api, record, self.config)

        methods = [m for (m, _) in api.calls]
        self.assertLess(methods.index("pane.process_info"), methods.index("pane.send_text"))
        self.assertEqual(
            self._params(api, "pane.send_text")[0],
            {"pane_id": "w1:p9", "text": "nvim README.md\n"},
        )
        self.assertEqual([p["program"] for p in result["relaunched"]], ["nvim"])
        self.assertEqual(result["relaunch_failed"], [])

    def test_a_filename_with_a_space_is_quoted(self):
        api = self._api()
        record = make_pane_record(
            dead_side="second",
            relaunch_intent={"kind": "process", "argv": ["nvim", "my notes.md"]},
        )
        restore.restore_pane_record(api, record, self.config)
        self.assertEqual(
            self._params(api, "pane.send_text")[0]["text"], "nvim 'my notes.md'\n"
        )

    def test_an_agent_is_resumed_and_the_not_restarted_caveat_drops(self):
        api = self._api()
        record = make_pane_record(
            dead_side="second",
            agent="claude",
            relaunch_intent={"kind": "agent", "agent": "claude", "session_id": "sess-1"},
        )
        result = restore.restore_pane_record(api, record, self.config)
        self.assertEqual(
            self._params(api, "pane.send_text")[0]["text"], "claude -r sess-1\n"
        )
        self.assertIsNone(result["agent"])

    def test_a_program_outside_the_allowlist_is_reported_not_run(self):
        api = self._api()
        record = make_pane_record(
            dead_side="second",
            relaunch_intent={"kind": "process", "argv": ["psql", "prod"]},
        )
        result = restore.restore_pane_record(api, record, self.config)
        self.assertNotIn("pane.send_text", [m for (m, _) in api.calls])
        self.assertNotIn("pane.process_info", [m for (m, _) in api.calls])
        self.assertEqual([p["program"] for p in result["not_relaunched"]], ["psql"])
        self.assertEqual(result["relaunched"], [])

    def test_a_shell_that_never_becomes_ready_is_reported_not_raised(self):
        api = self._api(ready=False)
        record = make_pane_record(
            dead_side="second",
            relaunch_intent={"kind": "process", "argv": ["nvim", "README.md"]},
        )
        with mock.patch.object(relaunch, "READY_TIMEOUT_S", 0.0):
            result = restore.restore_pane_record(api, record, self.config)

        self.assertEqual(result["new_pane_id"], "w1:p9")
        self.assertNotIn("pane.send_text", [m for (m, _) in api.calls])
        self.assertEqual([p["program"] for p in result["relaunch_failed"]], ["nvim"])
        self.assertEqual(result["relaunched"], [])

    def test_a_failing_send_text_is_reported_not_raised(self):
        api = self._api(extra={"pane.send_text": RuntimeError("nope")})
        record = make_pane_record(
            dead_side="second",
            relaunch_intent={"kind": "process", "argv": ["nvim", "README.md"]},
        )
        result = restore.restore_pane_record(api, record, self.config)
        self.assertEqual(result["new_pane_id"], "w1:p9")
        self.assertEqual([p["program"] for p in result["relaunch_failed"]], ["nvim"])

    def test_restore_process_false_types_nothing(self):
        api = self._api()
        record = make_pane_record(
            dead_side="second",
            relaunch_intent={"kind": "process", "argv": ["nvim", "README.md"]},
        )
        result = restore.restore_pane_record(api, record, config(restore_process=False))
        self.assertNotIn("pane.send_text", [m for (m, _) in api.calls])
        self.assertEqual(result["relaunched"], [])
        self.assertEqual(result["not_relaunched"], [])

    def test_a_replayed_process_retires_the_lost_launch_command_caveat(self):
        api = self._api()
        record = make_pane_record(
            dead_side="second",
            command=["nvim", "README.md"],
            relaunch_intent={"kind": "process", "argv": ["nvim", "README.md"]},
        )
        result = restore.restore_pane_record(api, record, self.config)
        self.assertFalse(result["command_lost"])

    def test_a_pane_with_nothing_running_still_reports_a_lost_command(self):
        api = self._api()
        record = make_pane_record(dead_side="second", command=["npm", "run", "dev"])
        result = restore.restore_pane_record(api, record, self.config)
        self.assertTrue(result["command_lost"])
        self.assertNotIn("pane.send_text", [m for (m, _) in api.calls])


class TestReopenAt(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix="herdr-undo-close-test-")
        self.config = dict(CONFIG)

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

    def test_pane_entry_is_dispatched_to_the_pane_path_and_then_removed(self):
        pane_entry = make_pane_record(closed_ms=2000)
        store.save_state(self.state_dir, {"version": 2, "index": {}, "closed": [pane_entry]})

        api = pane_api()
        result = restore.reopen_at(api, self.state_dir, self.config, None, index=0)

        self.assertEqual(result["kind"], "pane")
        self.assertEqual(result["new_pane_id"], "w1:p9")
        self.assertNotIn("workspace.list", [m for (m, _) in api.calls])
        self.assertEqual(store.load_state(self.state_dir)["closed"], [])

    def test_kind_is_part_of_the_removal_identity(self):
        # A pane and its own tab can share tab_id and closed_ms (one pass that
        # saw both deaths), so removal must not drop the wrong one.
        pane_entry = make_pane_record(tab_id="w1:t7", closed_ms=2000)
        tab_entry = make_record(tab_id="w1:t7", closed_ms=2000)
        tab_entry["kind"] = "tab"
        store.save_state(
            self.state_dir, {"version": 2, "index": {}, "closed": [pane_entry, tab_entry]}
        )

        restore.reopen_at(api=pane_api(), state_dir=self.state_dir, config=self.config,
                          env_workspace_id=None, index=0)

        saved = store.load_state(self.state_dir)
        self.assertEqual([store.entry_kind(c) for c in saved["closed"]], ["tab"])

    def test_pane_tab_gone_leaves_the_entry_in_the_stack(self):
        pane_entry = make_pane_record()
        store.save_state(self.state_dir, {"version": 2, "index": {}, "closed": [pane_entry]})

        api = pane_api(live_tab_ids=("w1:t9",))
        with self.assertRaises(restore.PaneTabGone):
            restore.reopen_at(api, self.state_dir, self.config, None, index=0)

        saved = store.load_state(self.state_dir)
        self.assertEqual(len(saved["closed"]), 1)

    def test_v1_kindless_entry_is_restored_as_a_tab(self):
        v1_entry = make_record(tab_id="w1:tA", closed_ms=2000)
        self.assertNotIn("kind", v1_entry)
        store.save_state(self.state_dir, {"version": 1, "index": {}, "closed": [v1_entry]})

        api = FakeApi(
            {
                "workspace.list": {"workspaces": [{"workspace_id": "w1"}]},
                "layout.apply": {"layout": {"tab_id": "w1:new"}},
                "tab.list": {"tabs": [{"workspace_id": "w1"}]},
                "tab.move": {},
            }
        )
        result = restore.reopen_at(api, self.state_dir, self.config, None, index=0)

        self.assertEqual(result["kind"], "tab")
        self.assertEqual(result["new_tab_id"], "w1:new")
        self.assertEqual(store.load_state(self.state_dir)["closed"], [])


if __name__ == "__main__":
    unittest.main()
