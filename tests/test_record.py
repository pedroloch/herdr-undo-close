"""Tests for herdr_undo_close.record: tree helpers, live-index computation,
death reconciliation, refresh, and recording what each pane is running. A
fake API stands in for the socket -- no network, no real Herdr process
involved, and the claude session lookup is always pointed at an empty temp
directory so no test reads the real ~/.claude."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from herdr_undo_close import record, relaunch, store


class ClaudeHomeIsolated(object):
    """Mixin for any test that can reach the claude session lookup: it points
    ~/.claude/projects at an empty temp directory. The real one must never be
    read by the suite, not even to find it missing."""

    def setUp(self):
        super(ClaudeHomeIsolated, self).setUp()
        projects = tempfile.mkdtemp(prefix="herdr-undo-close-claude-")
        self.addCleanup(shutil.rmtree, projects, True)
        patcher = mock.patch.object(
            relaunch, "claude_projects_dir", return_value=projects
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class FakeApi(object):
    """Records requests and answers from a canned script keyed by method.

    `script[method]` may be:
      - a callable taking params and returning a result dict, or
      - a plain result dict returned unconditionally, or
      - an Exception instance/class to be raised.
    """

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


def pane_node(pane_id=None, cwd=None, **extra):
    node = {"type": "pane"}
    if pane_id is not None:
        node["pane_id"] = pane_id
    if cwd is not None:
        node["cwd"] = cwd
    node.update(extra)
    return node


def split_node(direction, first, second, ratio=0.5):
    return {"type": "split", "direction": direction, "ratio": ratio, "first": first, "second": second}


class TestStripPaneIds(unittest.TestCase):
    def test_removes_pane_id_from_single_pane(self):
        node = pane_node(pane_id="w1:pA", cwd="/tmp")
        stripped = record.strip_pane_ids(node)
        self.assertNotIn("pane_id", stripped)
        self.assertEqual(stripped["cwd"], "/tmp")

    def test_recurses_into_split_children(self):
        tree = split_node("right", pane_node(pane_id="pA", cwd="/a"), pane_node(pane_id="pB", cwd="/b"))
        stripped = record.strip_pane_ids(tree)
        self.assertNotIn("pane_id", stripped["first"])
        self.assertNotIn("pane_id", stripped["second"])
        self.assertEqual(stripped["direction"], "right")
        self.assertEqual(stripped["ratio"], 0.5)

    def test_pane_without_pane_id_is_left_alone_otherwise(self):
        node = pane_node(cwd="/agent-running")
        stripped = record.strip_pane_ids(node)
        self.assertEqual(stripped, {"type": "pane", "cwd": "/agent-running"})

    def test_none_returns_none(self):
        self.assertIsNone(record.strip_pane_ids(None))

    def test_does_not_mutate_input(self):
        original = split_node("down", pane_node(pane_id="pA"), pane_node(pane_id="pB"))
        record.strip_pane_ids(original)
        self.assertEqual(original["first"]["pane_id"], "pA")
        self.assertEqual(original["second"]["pane_id"], "pB")


class TestFlattenPanes(unittest.TestCase):
    def test_single_pane(self):
        panes = record.flatten_panes(pane_node(pane_id="pA", cwd="/a"))
        self.assertEqual(panes, [{"pane_id": "pA", "cwd": "/a"}])

    def test_nested_tree_preserves_first_before_second_order(self):
        tree = split_node(
            "right",
            pane_node(pane_id="pA", cwd="/a"),
            split_node("down", pane_node(pane_id="pB", cwd="/b"), pane_node(pane_id="pC", cwd="/c")),
        )
        panes = record.flatten_panes(tree)
        self.assertEqual([p["pane_id"] for p in panes], ["pA", "pB", "pC"])

    def test_only_keeps_pane_id_and_cwd(self):
        node = {"type": "pane", "pane_id": "pA", "cwd": "/a", "command": ["bash"], "env": {"X": "1"}}
        panes = record.flatten_panes(node)
        self.assertEqual(panes, [{"pane_id": "pA", "cwd": "/a"}])

    def test_empty_tree_yields_no_panes(self):
        self.assertEqual(record.flatten_panes(None), [])

    def test_pane_entries_and_pane_nodes_are_the_same_order(self):
        # Load-bearing invariant: a record's `panes` list is this function's
        # output, and the tab restore zips it positionally against
        # flatten_pane_nodes of the tree to decide which node gets a command.
        tree = split_node(
            "right",
            pane_node(pane_id="pA", cwd="/a"),
            split_node(
                "down",
                pane_node(pane_id="pB", cwd="/b"),
                split_node("right", pane_node(pane_id="pC", cwd="/c"), pane_node(pane_id="pD", cwd="/d")),
            ),
        )
        self.assertEqual(
            [p["pane_id"] for p in record.flatten_panes(tree)],
            [n["pane_id"] for n in record.flatten_pane_nodes(tree)],
        )

    def test_flatten_pane_nodes_hands_back_the_nodes_themselves(self):
        # The tab restore mutates them, so copies would silently do nothing.
        leaf = pane_node(pane_id="pA", cwd="/a")
        tree = split_node("right", leaf, pane_node(pane_id="pB"))
        self.assertIs(record.flatten_pane_nodes(tree)[0], leaf)

    def test_malformed_children_are_skipped_not_fatal(self):
        tree = split_node("right", pane_node(pane_id="pA"), "not-a-node")
        self.assertEqual([p["pane_id"] for p in record.flatten_panes(tree)], ["pA"])


class TestEnrichWithAgents(unittest.TestCase):
    def test_adds_agent_fields_for_matching_pane(self):
        panes = [{"pane_id": "pA", "cwd": "/a"}, {"pane_id": "pB", "cwd": "/b"}]
        agents = [{"pane_id": "pA", "agent": "claude", "agent_status": "working"}]
        record.enrich_with_agents(panes, agents)
        self.assertEqual(panes[0]["agent"], "claude")
        self.assertEqual(panes[0]["agent_status"], "working")
        self.assertNotIn("agent", panes[1])

    def test_omits_agent_session_when_absent(self):
        panes = [{"pane_id": "pA"}]
        agents = [{"pane_id": "pA", "agent": "claude", "agent_status": "idle"}]
        record.enrich_with_agents(panes, agents)
        self.assertNotIn("agent_session", panes[0])

    def test_includes_agent_session_when_present(self):
        panes = [{"pane_id": "pA"}]
        agents = [{"pane_id": "pA", "agent": "claude", "agent_status": "idle", "agent_session": {"id": "s1"}}]
        record.enrich_with_agents(panes, agents)
        self.assertEqual(panes[0]["agent_session"], {"id": "s1"})


class TestBuildLiveIndex(unittest.TestCase):
    def test_index_is_position_among_same_workspace_tabs(self):
        tabs = [
            {"workspace_id": "w1", "tab_id": "w1:t1", "number": 1, "label": "a"},
            {"workspace_id": "w1", "tab_id": "w1:t2", "number": 12, "label": "b"},
            {"workspace_id": "w2", "tab_id": "w2:t1", "number": 1, "label": "c"},
        ]
        live = record.build_live_index(tabs)
        self.assertEqual(live["w1:t1"]["index"], 0)
        self.assertEqual(live["w1:t2"]["index"], 1)
        self.assertEqual(live["w2:t1"]["index"], 0)

    def test_number_field_is_ignored(self):
        tabs = [{"workspace_id": "w1", "tab_id": "w1:t2", "number": 12, "label": "b"}]
        live = record.build_live_index(tabs)
        self.assertEqual(live["w1:t2"]["index"], 0)


def make_record(tab_id, workspace_id="w1", refreshed_ms=1):
    """A version 2 index record: unstripped `tree`, panes carrying terminal_id."""
    return {
        "tab_id": tab_id,
        "workspace_id": workspace_id,
        "workspace_label": "ws",
        "label": "probe",
        "index": 0,
        "tree": {"type": "pane", "pane_id": "pA", "cwd": "/x"},
        "panes": [{"pane_id": "pA", "terminal_id": "term_a", "cwd": "/x"}],
        "refreshed_ms": refreshed_ms,
    }


def live(tab_id="w1:t1", workspace_id="w1", label="probe", index=0):
    return {tab_id: {"workspace_id": workspace_id, "label": label, "index": index}}


class TestReconcileDeaths(unittest.TestCase):
    def test_tab_missing_from_live_moves_to_closed(self):
        state = {"index": {"w1:t1": make_record("w1:t1")}, "closed": []}
        dead = record.reconcile_deaths(state, live_ids={}, forced_tab_id=None, now_ms=999)
        self.assertEqual(dead, ["w1:t1"])
        self.assertNotIn("w1:t1", state["index"])
        self.assertEqual(len(state["closed"]), 1)
        self.assertEqual(state["closed"][0]["tab_id"], "w1:t1")
        self.assertEqual(state["closed"][0]["closed_ms"], 999)

    def test_closed_tab_entry_is_kind_tab_with_a_stripped_root(self):
        rec = make_record("w1:t1")
        rec["tree"] = split_node("right", pane_node(pane_id="pA", cwd="/a"), pane_node(pane_id="pB", cwd="/b"))
        state = {"index": {"w1:t1": rec}, "closed": []}
        record.reconcile_deaths(state, live_ids={}, forced_tab_id=None, now_ms=1)
        entry = state["closed"][0]
        self.assertEqual(entry["kind"], "tab")
        self.assertNotIn("tree", entry)
        self.assertNotIn("pane_id", entry["root"]["first"])
        self.assertNotIn("pane_id", entry["root"]["second"])

    def test_v1_index_record_without_tree_keeps_its_stripped_root(self):
        rec = make_record("w1:t1")
        del rec["tree"]
        rec["root"] = {"type": "pane", "cwd": "/x"}
        state = {"index": {"w1:t1": rec}, "closed": []}
        record.reconcile_deaths(state, live_ids={}, forced_tab_id=None, now_ms=1)
        self.assertEqual(state["closed"][0]["root"], {"type": "pane", "cwd": "/x"})

    def test_forced_tab_closed_id_dies_even_if_still_listed(self):
        state = {"index": {"w1:t1": make_record("w1:t1")}, "closed": []}
        live_ids = {"w1:t1": {"workspace_id": "w1", "label": "probe", "index": 0}}
        dead = record.reconcile_deaths(state, live_ids=live_ids, forced_tab_id="w1:t1", now_ms=1000)
        self.assertEqual(dead, ["w1:t1"])
        self.assertNotIn("w1:t1", state["index"])

    def test_live_tab_is_left_alone(self):
        state = {"index": {"w1:t1": make_record("w1:t1")}, "closed": []}
        live_ids = {"w1:t1": {"workspace_id": "w1", "label": "probe", "index": 0}}
        dead = record.reconcile_deaths(state, live_ids=live_ids, forced_tab_id=None, now_ms=1000)
        self.assertEqual(dead, [])
        self.assertIn("w1:t1", state["index"])

    def test_duplicates_never_accumulate(self):
        state = {
            "index": {"w1:t1": make_record("w1:t1")},
            "closed": [make_record("w1:t1")],
        }
        record.reconcile_deaths(state, live_ids={}, forced_tab_id=None, now_ms=2000)
        matching = [c for c in state["closed"] if c["tab_id"] == "w1:t1"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["closed_ms"], 2000)

    def test_newest_first_ordering_across_passes(self):
        state = {
            "index": {"w1:t1": make_record("w1:t1"), "w1:t2": make_record("w1:t2")},
            "closed": [],
        }
        record.reconcile_deaths(state, live_ids={"w1:t2": {"workspace_id": "w1", "label": "b", "index": 0}}, forced_tab_id=None, now_ms=1000)
        record.reconcile_deaths(state, live_ids={}, forced_tab_id=None, now_ms=2000)
        self.assertEqual(state["closed"][0]["tab_id"], "w1:t2")
        self.assertEqual(state["closed"][1]["tab_id"], "w1:t1")


class TestRefreshLive(ClaudeHomeIsolated, unittest.TestCase):
    def test_successful_export_upserts_record(self):
        state = {"index": {}, "closed": []}
        tree = pane_node(pane_id="pA", cwd="/proj")
        api = FakeApi({"layout.export": {"layout": {"root": tree}}})
        live_ids = {"w1:t1": {"workspace_id": "w1", "label": "probe", "index": 3}}
        record.refresh_live(state, api, live_ids, {"w1": "maestro"}, agents=[], now_ms=555)
        rec = state["index"]["w1:t1"]
        self.assertEqual(rec["workspace_label"], "maestro")
        self.assertEqual(rec["index"], 3)
        self.assertEqual(rec["refreshed_ms"], 555)
        # The index keeps pane ids: pane-death records are computed from them.
        self.assertEqual(rec["tree"]["pane_id"], "pA")
        self.assertNotIn("root", rec)
        self.assertEqual(rec["panes"], [{"pane_id": "pA", "cwd": "/proj"}])

    def test_terminal_id_is_attached_from_pane_list(self):
        state = {"index": {}, "closed": []}
        tree = pane_node(pane_id="pA")
        api = FakeApi({"layout.export": {"layout": {"root": tree}}})
        live_ids = {"w1:t1": {"workspace_id": "w1", "label": "probe", "index": 0}}
        panes_by_id = {"pA": {"pane_id": "pA", "terminal_id": "term_a", "cwd": "/proj", "label": "build"}}
        record.refresh_live(state, api, live_ids, {}, agents=[], now_ms=1, panes_by_id=panes_by_id)
        pane = state["index"]["w1:t1"]["panes"][0]
        self.assertEqual(pane["terminal_id"], "term_a")
        self.assertEqual(pane["label"], "build")
        self.assertEqual(pane["cwd"], "/proj")

    def test_failed_export_leaves_existing_record_untouched(self):
        existing = make_record("w1:t1", refreshed_ms=42)
        state = {"index": {"w1:t1": existing}, "closed": []}
        api = FakeApi({"layout.export": RuntimeError("boom")})
        live_ids = {"w1:t1": {"workspace_id": "w1", "label": "probe", "index": 0}}
        record.refresh_live(state, api, live_ids, {}, agents=[], now_ms=999)
        self.assertEqual(state["index"]["w1:t1"], existing)

    def test_empty_export_leaves_existing_record_untouched(self):
        existing = make_record("w1:t1", refreshed_ms=42)
        state = {"index": {"w1:t1": existing}, "closed": []}
        api = FakeApi({"layout.export": {"layout": {"root": None}}})
        live_ids = {"w1:t1": {"workspace_id": "w1", "label": "probe", "index": 0}}
        record.refresh_live(state, api, live_ids, {}, agents=[], now_ms=999)
        self.assertEqual(state["index"]["w1:t1"], existing)

    def test_agents_attached_from_single_agent_list_call(self):
        state = {"index": {}, "closed": []}
        tree = pane_node(pane_id="pA", cwd="/proj")
        api = FakeApi({"layout.export": {"layout": {"root": tree}}})
        live_ids = {"w1:t1": {"workspace_id": "w1", "label": "probe", "index": 0}}
        agents = [{"pane_id": "pA", "agent": "claude", "agent_status": "working"}]
        record.refresh_live(state, api, live_ids, {}, agents=agents, now_ms=1)
        self.assertEqual(state["index"]["w1:t1"]["panes"][0]["agent"], "claude")

    def _two_pane_export_api(self, process_info):
        # process_info is the INNER payload (or a callable/Exception producing
        # one) a caller wants to see; the fake wraps it in the same envelope
        # the real socket sends, since api.call("pane.process_info", ...)
        # never hands back a flat payload (see relaunch.unwrap_process_info).
        if isinstance(process_info, Exception) or (
            isinstance(process_info, type) and issubclass(process_info, Exception)
        ):
            answer = process_info
        elif callable(process_info):
            answer = lambda params: {  # noqa: E731
                "type": "pane_process_info",
                "process_info": process_info(params),
            }
        else:
            answer = {"type": "pane_process_info", "process_info": process_info}
        return FakeApi(
            {
                "layout.export": {
                    "layout": {
                        "root": split_node(
                            "right",
                            pane_node(pane_id="pA", cwd="/proj"),
                            pane_node(pane_id="pB", cwd="/proj/sub"),
                        )
                    }
                },
                "pane.process_info": answer,
            }
        )

    def test_a_running_program_is_recorded_on_the_pane_that_runs_it(self):
        state = {"index": {}, "closed": []}
        api = self._two_pane_export_api(
            lambda params: {
                "shell_pid": 10,
                "foreground_processes": [
                    {"pid": 11, "name": "nvim", "argv": ["nvim", "README.md"]}
                    if params["pane_id"] == "pA"
                    else {"pid": 10, "name": "-zsh", "argv": ["-zsh"]}
                ],
            }
        )
        live_ids = {"w1:t1": {"workspace_id": "w1", "label": "probe", "index": 0}}
        record.refresh_live(state, api, live_ids, {}, agents=[], now_ms=1)

        panes = state["index"]["w1:t1"]["panes"]
        self.assertEqual(panes[0]["relaunch"], {"kind": "process", "argv": ["nvim", "README.md"]})
        self.assertNotIn("relaunch", panes[1])

    def test_the_tree_is_never_rewritten_with_a_command_at_record_time(self):
        state = {"index": {}, "closed": []}
        api = self._two_pane_export_api(
            {
                "shell_pid": 10,
                "foreground_processes": [{"pid": 11, "name": "nvim", "argv": ["nvim"]}],
            }
        )
        live_ids = {"w1:t1": {"workspace_id": "w1", "label": "probe", "index": 0}}
        record.refresh_live(state, api, live_ids, {}, agents=[], now_ms=1)
        tree = state["index"]["w1:t1"]["tree"]
        self.assertNotIn("command", tree["first"])
        self.assertNotIn("command", tree["second"])

    def test_an_agent_pane_is_never_asked_for_process_info(self):
        state = {"index": {}, "closed": []}
        api = self._two_pane_export_api(
            {"shell_pid": 10, "foreground_processes": [{"pid": 10, "argv": ["-zsh"]}]}
        )
        live_ids = {"w1:t1": {"workspace_id": "w1", "label": "probe", "index": 0}}
        agents = [
            {
                "pane_id": "pA",
                "agent": "opencode",
                "agent_session": {"kind": "id", "value": "ses_1"},
            }
        ]
        record.refresh_live(state, api, live_ids, {}, agents=agents, now_ms=1)

        panes = state["index"]["w1:t1"]["panes"]
        self.assertEqual(
            panes[0]["relaunch"],
            {"kind": "agent", "agent": "opencode", "session_id": "ses_1"},
        )
        queried = [p["pane_id"] for (m, p) in api.calls if m == "pane.process_info"]
        self.assertEqual(queried, ["pB"])

    def test_a_failing_process_info_costs_the_intent_and_nothing_else(self):
        state = {"index": {}, "closed": []}
        api = self._two_pane_export_api(RuntimeError("no such method"))
        live_ids = {"w1:t1": {"workspace_id": "w1", "label": "probe", "index": 0}}
        record.refresh_live(state, api, live_ids, {}, agents=[], now_ms=1)

        panes = state["index"]["w1:t1"]["panes"]
        self.assertEqual([p["pane_id"] for p in panes], ["pA", "pB"])
        self.assertNotIn("relaunch", panes[0])
        self.assertNotIn("relaunch", panes[1])

    def test_restore_process_false_never_asks_what_is_running(self):
        state = {"index": {}, "closed": []}
        api = self._two_pane_export_api(
            {
                "shell_pid": 10,
                "foreground_processes": [{"pid": 11, "name": "nvim", "argv": ["nvim"]}],
            }
        )
        live_ids = {"w1:t1": {"workspace_id": "w1", "label": "probe", "index": 0}}
        record.refresh_live(
            state, api, live_ids, {}, agents=[], now_ms=1, restore_process=False
        )
        self.assertNotIn("pane.process_info", [m for (m, _) in api.calls])
        for pane in state["index"]["w1:t1"]["panes"]:
            self.assertNotIn("relaunch", pane)


class TestFindPaneNode(unittest.TestCase):
    def setUp(self):
        # split(right,0.4, A, split(down,0.7, B, C))
        self.inner = split_node("down", pane_node(pane_id="pB"), pane_node(pane_id="pC"), ratio=0.7)
        self.tree = split_node("right", pane_node(pane_id="pA"), self.inner, ratio=0.4)

    def test_root_pane_has_no_parent(self):
        node, parent, side = record.find_pane_node(pane_node(pane_id="pA"), "pA")
        self.assertEqual(node["pane_id"], "pA")
        self.assertIsNone(parent)
        self.assertIsNone(side)

    def test_finds_first_side_of_the_root_split(self):
        node, parent, side = record.find_pane_node(self.tree, "pA")
        self.assertEqual(node["pane_id"], "pA")
        self.assertEqual(side, "first")
        self.assertEqual(parent["ratio"], 0.4)

    def test_finds_a_nested_pane_with_its_own_parent_split(self):
        node, parent, side = record.find_pane_node(self.tree, "pC")
        self.assertEqual(node["pane_id"], "pC")
        self.assertEqual(side, "second")
        self.assertIs(parent, self.inner)

    def test_missing_pane_returns_none(self):
        self.assertIsNone(record.find_pane_node(self.tree, "pZ"))

    def test_malformed_node_returns_none(self):
        self.assertIsNone(record.find_pane_node(None, "pA"))
        self.assertIsNone(record.find_pane_node({"type": "weird"}, "pA"))


class TestSiblingPaneIds(unittest.TestCase):
    def test_leaf_sibling_of_a_first_side_death(self):
        self.assertEqual(record.sibling_pane_ids(pane_node(pane_id="pB"), "first"), ["pB"])

    def test_leaf_sibling_of_a_second_side_death(self):
        self.assertEqual(record.sibling_pane_ids(pane_node(pane_id="pB"), "second"), ["pB"])

    def test_subtree_sibling_of_a_first_side_death_keeps_tree_order(self):
        sibling = split_node("down", pane_node(pane_id="pB"), pane_node(pane_id="pC"))
        self.assertEqual(record.sibling_pane_ids(sibling, "first"), ["pB", "pC"])

    def test_subtree_sibling_of_a_second_side_death_is_reversed(self):
        sibling = split_node("down", pane_node(pane_id="pA"), pane_node(pane_id="pB"))
        self.assertEqual(record.sibling_pane_ids(sibling, "second"), ["pB", "pA"])

    def test_missing_sibling_yields_no_ids(self):
        self.assertEqual(record.sibling_pane_ids(None, "first"), [])


class TestBuildPaneRecord(unittest.TestCase):
    def setUp(self):
        self.tab_record = make_record("w1:t1")

    def test_captures_parent_split_and_pane_details(self):
        tree = split_node(
            "right",
            pane_node(pane_id="pA", cwd="/proj", label="build", env={"X": "1"}, command=["npm", "run", "dev"]),
            pane_node(pane_id="pB", cwd="/proj/sub"),
            ratio=0.7,
        )
        pane_entry = {"pane_id": "pA", "terminal_id": "term_a", "cwd": "/proj", "agent": "claude"}
        entry = record.build_pane_record(self.tab_record, pane_entry, tree, now_ms=42)
        self.assertEqual(entry["kind"], "pane")
        self.assertEqual(entry["pane_id"], "pA")
        self.assertEqual(entry["terminal_id"], "term_a")
        self.assertEqual(entry["tab_id"], "w1:t1")
        self.assertEqual(entry["tab_label"], "probe")
        self.assertEqual(entry["cwd"], "/proj")
        self.assertEqual(entry["label"], "build")
        self.assertEqual(entry["env"], {"X": "1"})
        self.assertEqual(entry["command"], ["npm", "run", "dev"])
        self.assertEqual(entry["agent"], "claude")
        self.assertEqual(entry["closed_ms"], 42)
        self.assertEqual(
            entry["parent"],
            {
                "direction": "right",
                "ratio": 0.7,
                "dead_side": "first",
                "sibling_pane_ids": ["pB"],
                "sibling_is_leaf": True,
            },
        )

    def test_agent_key_omitted_when_the_pane_had_none(self):
        tree = split_node("down", pane_node(pane_id="pA"), pane_node(pane_id="pB"))
        entry = record.build_pane_record(
            self.tab_record, {"pane_id": "pA", "terminal_id": "term_a"}, tree, now_ms=1
        )
        self.assertNotIn("agent", entry)

    def test_subtree_sibling_is_not_a_leaf(self):
        tree = split_node(
            "right",
            pane_node(pane_id="pA"),
            split_node("down", pane_node(pane_id="pB"), pane_node(pane_id="pC"), ratio=0.7),
            ratio=0.4,
        )
        entry = record.build_pane_record(
            self.tab_record, {"pane_id": "pA", "terminal_id": "term_a"}, tree, now_ms=1
        )
        self.assertFalse(entry["parent"]["sibling_is_leaf"])
        self.assertEqual(entry["parent"]["sibling_pane_ids"], ["pB", "pC"])

    def test_only_pane_of_a_tab_has_no_parent_split_and_yields_nothing(self):
        tree = pane_node(pane_id="pA", cwd="/x")
        entry = record.build_pane_record(
            self.tab_record, {"pane_id": "pA", "terminal_id": "term_a"}, tree, now_ms=1
        )
        self.assertIsNone(entry)

    def test_the_relaunch_intent_survives_into_the_closed_entry(self):
        # The pane is gone by the time this runs, so the intent recorded while
        # it lived is the only copy there will ever be.
        tree = split_node("right", pane_node(pane_id="pA"), pane_node(pane_id="pB"))
        pane_entry = {
            "pane_id": "pA",
            "terminal_id": "term_a",
            "relaunch": {"kind": "process", "argv": ["nvim", "README.md"]},
        }
        entry = record.build_pane_record(self.tab_record, pane_entry, tree, now_ms=1)
        self.assertEqual(entry["relaunch"], {"kind": "process", "argv": ["nvim", "README.md"]})

    def test_no_relaunch_key_when_the_pane_was_an_idle_shell(self):
        tree = split_node("right", pane_node(pane_id="pA"), pane_node(pane_id="pB"))
        entry = record.build_pane_record(
            self.tab_record, {"pane_id": "pA", "terminal_id": "term_a"}, tree, now_ms=1
        )
        self.assertNotIn("relaunch", entry)

    def test_pane_absent_from_the_tree_yields_nothing(self):
        tree = split_node("right", pane_node(pane_id="pA"), pane_node(pane_id="pB"))
        entry = record.build_pane_record(
            self.tab_record, {"pane_id": "pZ", "terminal_id": "term_z"}, tree, now_ms=1
        )
        self.assertIsNone(entry)


def two_pane_tab(tab_id="w1:t1", ratio=0.7, direction="right"):
    rec = make_record(tab_id)
    rec["tree"] = split_node(
        direction,
        pane_node(pane_id="pA", cwd="/proj"),
        pane_node(pane_id="pB", cwd="/proj/sub"),
        ratio=ratio,
    )
    rec["panes"] = [
        {"pane_id": "pA", "terminal_id": "term_a", "cwd": "/proj"},
        {"pane_id": "pB", "terminal_id": "term_b", "cwd": "/proj/sub"},
    ]
    return rec


class TestReconcilePaneDeaths(unittest.TestCase):
    def _run(self, state, live_pane_ids, live_terminal_ids, live_ids=None):
        return record.reconcile_pane_deaths(
            state,
            live_ids if live_ids is not None else live(),
            set(live_pane_ids),
            set(live_terminal_ids),
            now_ms=500,
        )

    def test_records_a_death_when_both_id_and_terminal_are_gone(self):
        state = {"index": {"w1:t1": two_pane_tab()}, "closed": []}
        entries = self._run(state, ["pB"], ["term_b"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(state["closed"][0]["pane_id"], "pA")
        self.assertEqual(state["closed"][0]["parent"]["ratio"], 0.7)
        self.assertEqual(state["closed"][0]["closed_ms"], 500)

    def test_no_record_when_the_id_is_gone_but_the_terminal_is_alive(self):
        # A cross-workspace move: pane_id changed, terminal_id followed the pane.
        state = {"index": {"w1:t1": two_pane_tab()}, "closed": []}
        entries = self._run(state, ["w17:p1", "pB"], ["term_a", "term_b"])
        self.assertEqual(entries, [])
        self.assertEqual(state["closed"], [])

    def test_no_record_when_the_pane_is_simply_still_alive(self):
        state = {"index": {"w1:t1": two_pane_tab()}, "closed": []}
        self.assertEqual(self._run(state, ["pA", "pB"], ["term_a", "term_b"]), [])

    def test_no_record_when_the_tab_died_in_the_same_pass(self):
        state = {"index": {"w1:t1": two_pane_tab()}, "closed": []}
        entries = self._run(state, [], [], live_ids={})
        self.assertEqual(entries, [])

    def test_no_record_for_a_tab_whose_only_pane_died(self):
        rec = make_record("w1:t1")  # tree is a single pane, no parent split
        state = {"index": {"w1:t1": rec}, "closed": []}
        self.assertEqual(self._run(state, [], []), [])

    def test_no_record_when_the_previous_entry_has_no_terminal_id(self):
        rec = two_pane_tab()
        del rec["panes"][0]["terminal_id"]
        state = {"index": {"w1:t1": rec}, "closed": []}
        entries = self._run(state, ["pB"], ["term_b"])
        self.assertEqual(entries, [])

    def test_index_entry_without_a_tree_is_skipped_not_fatal(self):
        rec = two_pane_tab()
        del rec["tree"]
        rec["root"] = {"type": "pane", "cwd": "/x"}
        state = {"index": {"w1:t1": rec}, "closed": []}
        self.assertEqual(self._run(state, ["pB"], ["term_b"]), [])

    def test_the_same_death_is_never_stacked_twice(self):
        state = {"index": {"w1:t1": two_pane_tab()}, "closed": []}
        self._run(state, ["pB"], ["term_b"])
        self._run(state, ["pB"], ["term_b"])
        self.assertEqual(len(state["closed"]), 1)


class TestPruneOrphanPaneEntries(unittest.TestCase):
    def _pane_entry(self, tab_id="w1:t1", pane_id="pA"):
        return {"kind": "pane", "pane_id": pane_id, "tab_id": tab_id, "closed_ms": 1}

    def test_pane_entry_of_a_dead_tab_is_dropped(self):
        state = {"index": {}, "closed": [self._pane_entry()]}
        self.assertEqual(record.prune_orphan_pane_entries(state, live_ids={}), 1)
        self.assertEqual(state["closed"], [])

    def test_pane_entry_of_a_live_tab_survives(self):
        state = {"index": {}, "closed": [self._pane_entry()]}
        self.assertEqual(record.prune_orphan_pane_entries(state, live_ids=live()), 0)
        self.assertEqual(len(state["closed"]), 1)

    def test_tab_entries_are_never_pruned(self):
        state = {"index": {}, "closed": [{"kind": "tab", "tab_id": "w1:t1", "closed_ms": 1}]}
        record.prune_orphan_pane_entries(state, live_ids={})
        self.assertEqual(len(state["closed"]), 1)

    def test_v1_kindless_entries_are_treated_as_tabs_and_kept(self):
        state = {"index": {}, "closed": [{"tab_id": "w1:t1", "closed_ms": 1}]}
        record.prune_orphan_pane_entries(state, live_ids={})
        self.assertEqual(len(state["closed"]), 1)


class TestRunRecordPass(ClaudeHomeIsolated, unittest.TestCase):
    def setUp(self):
        super(TestRunRecordPass, self).setUp()
        self.state_dir = tempfile.mkdtemp(prefix="herdr-undo-close-test-")
        self.addCleanup(shutil.rmtree, self.state_dir, True)

    def test_full_pass_trims_closed_to_max_entries(self):
        pre_closed = [make_record("dead%d" % i, refreshed_ms=i) for i in range(5)]
        store.save_state(self.state_dir, {"version": 1, "index": {}, "closed": pre_closed})

        api = FakeApi(
            {
                "tab.list": {"tabs": []},
                "workspace.list": {"workspaces": []},
                "agent.list": {"agents": []},
            }
        )
        record.run_record_pass(api, self.state_dir, max_entries=2)

        saved = store.load_state(self.state_dir)
        self.assertEqual(len(saved["closed"]), 2)

    def test_full_pass_records_a_live_tab_and_retires_a_dead_one(self):
        store.save_state(
            self.state_dir,
            {"version": 1, "index": {"w1:old": make_record("w1:old")}, "closed": []},
        )
        tree = pane_node(pane_id="pA", cwd="/proj")
        api = FakeApi(
            {
                "tab.list": {"tabs": [{"workspace_id": "w1", "tab_id": "w1:new", "number": 1, "label": "new"}]},
                "workspace.list": {"workspaces": [{"workspace_id": "w1", "label": "maestro"}]},
                "agent.list": {"agents": []},
                "layout.export": {"layout": {"root": tree}},
            }
        )
        record.run_record_pass(api, self.state_dir, max_entries=25)

        saved = store.load_state(self.state_dir)
        self.assertIn("w1:new", saved["index"])
        self.assertEqual(saved["index"]["w1:new"]["workspace_label"], "maestro")
        self.assertEqual([c["tab_id"] for c in saved["closed"]], ["w1:old"])

    def test_forced_closed_tab_still_listed_ends_up_closed_not_reindexed(self):
        store.save_state(
            self.state_dir,
            {"version": 1, "index": {"w1:t1": make_record("w1:t1")}, "closed": []},
        )
        tree = pane_node(pane_id="pA", cwd="/proj")
        api = FakeApi(
            {
                "tab.list": {"tabs": [{"workspace_id": "w1", "tab_id": "w1:t1", "number": 1, "label": "probe"}]},
                "workspace.list": {"workspaces": [{"workspace_id": "w1", "label": "maestro"}]},
                "agent.list": {"agents": []},
                "layout.export": {"layout": {"root": tree}},
            }
        )
        record.run_record_pass(api, self.state_dir, max_entries=25, forced_tab_id="w1:t1")

        saved = store.load_state(self.state_dir)
        self.assertNotIn("w1:t1", saved["index"])
        self.assertEqual([c["tab_id"] for c in saved["closed"]], ["w1:t1"])

    def _live_tab_api(self, live_panes, export_root, tabs=None):
        return FakeApi(
            {
                "tab.list": {
                    "tabs": tabs
                    if tabs is not None
                    else [{"workspace_id": "w1", "tab_id": "w1:t1", "number": 1, "label": "probe"}]
                },
                "workspace.list": {"workspaces": [{"workspace_id": "w1", "label": "maestro"}]},
                "pane.list": {"panes": live_panes},
                "agent.list": {"agents": []},
                "layout.export": {"layout": {"root": export_root}},
            }
        )

    def test_full_pass_records_a_pane_death_in_a_surviving_tab(self):
        store.save_state(
            self.state_dir,
            {"version": 2, "index": {"w1:t1": two_pane_tab()}, "closed": []},
        )
        api = self._live_tab_api(
            [{"pane_id": "pB", "terminal_id": "term_b", "cwd": "/proj/sub"}],
            pane_node(pane_id="pB", cwd="/proj/sub"),
        )
        record.run_record_pass(api, self.state_dir, max_entries=25)

        saved = store.load_state(self.state_dir)
        self.assertEqual(saved["version"], 2)
        self.assertEqual(len(saved["closed"]), 1)
        entry = saved["closed"][0]
        self.assertEqual(entry["kind"], "pane")
        self.assertEqual(entry["pane_id"], "pA")
        self.assertEqual(entry["tab_id"], "w1:t1")
        self.assertEqual(entry["parent"]["dead_side"], "first")
        self.assertEqual(entry["parent"]["sibling_pane_ids"], ["pB"])
        # The tab itself stays tracked, now with only the surviving pane.
        self.assertEqual(saved["index"]["w1:t1"]["tree"]["pane_id"], "pB")
        self.assertEqual(saved["index"]["w1:t1"]["panes"][0]["terminal_id"], "term_b")

    def test_full_pass_carries_the_relaunch_intent_into_the_pane_death(self):
        rec = two_pane_tab()
        rec["panes"][0]["relaunch"] = {"kind": "process", "argv": ["nvim", "README.md"]}
        store.save_state(self.state_dir, {"version": 2, "index": {"w1:t1": rec}, "closed": []})
        api = self._live_tab_api(
            [{"pane_id": "pB", "terminal_id": "term_b", "cwd": "/proj/sub"}],
            pane_node(pane_id="pB", cwd="/proj/sub"),
        )
        record.run_record_pass(api, self.state_dir, max_entries=25)

        entry = store.load_state(self.state_dir)["closed"][0]
        self.assertEqual(entry["pane_id"], "pA")
        self.assertEqual(entry["relaunch"], {"kind": "process", "argv": ["nvim", "README.md"]})

    def test_full_pass_records_what_a_surviving_pane_is_running(self):
        store.save_state(self.state_dir, {"version": 2, "index": {}, "closed": []})
        api = self._live_tab_api(
            [{"pane_id": "pA", "terminal_id": "term_a", "cwd": "/proj"}],
            pane_node(pane_id="pA", cwd="/proj"),
        )
        api.script["pane.process_info"] = {
            "type": "pane_process_info",
            "process_info": {
                "shell_pid": 10,
                "foreground_processes": [{"pid": 11, "name": "nvim", "argv": ["nvim", "a.md"]}],
            },
        }
        record.run_record_pass(api, self.state_dir, max_entries=25)

        pane = store.load_state(self.state_dir)["index"]["w1:t1"]["panes"][0]
        self.assertEqual(pane["relaunch"], {"kind": "process", "argv": ["nvim", "a.md"]})

    def test_restore_process_false_records_no_intents_in_a_full_pass(self):
        store.save_state(self.state_dir, {"version": 2, "index": {}, "closed": []})
        api = self._live_tab_api(
            [{"pane_id": "pA", "terminal_id": "term_a", "cwd": "/proj"}],
            pane_node(pane_id="pA", cwd="/proj"),
        )
        api.script["pane.process_info"] = {
            "type": "pane_process_info",
            "process_info": {
                "shell_pid": 10,
                "foreground_processes": [{"pid": 11, "name": "nvim", "argv": ["nvim", "a.md"]}],
            },
        }
        record.run_record_pass(api, self.state_dir, max_entries=25, restore_process=False)

        pane = store.load_state(self.state_dir)["index"]["w1:t1"]["panes"][0]
        self.assertNotIn("relaunch", pane)
        self.assertNotIn("pane.process_info", [m for (m, _) in api.calls])

    def test_undo_panes_false_records_no_pane_deaths(self):
        store.save_state(
            self.state_dir,
            {"version": 2, "index": {"w1:t1": two_pane_tab()}, "closed": []},
        )
        api = self._live_tab_api(
            [{"pane_id": "pB", "terminal_id": "term_b", "cwd": "/proj/sub"}],
            pane_node(pane_id="pB", cwd="/proj/sub"),
        )
        record.run_record_pass(api, self.state_dir, max_entries=25, undo_panes=False)

        saved = store.load_state(self.state_dir)
        self.assertEqual(saved["closed"], [])
        self.assertEqual(saved["index"]["w1:t1"]["tree"]["pane_id"], "pB")

    def test_pane_death_is_stacked_ahead_of_a_tab_death_from_the_same_pass(self):
        store.save_state(
            self.state_dir,
            {
                "version": 2,
                "index": {"w1:t1": two_pane_tab(), "w1:t2": make_record("w1:t2")},
                "closed": [],
            },
        )
        api = self._live_tab_api(
            [{"pane_id": "pB", "terminal_id": "term_b", "cwd": "/proj/sub"}],
            pane_node(pane_id="pB", cwd="/proj/sub"),
        )
        record.run_record_pass(api, self.state_dir, max_entries=25)

        saved = store.load_state(self.state_dir)
        self.assertEqual([c["kind"] for c in saved["closed"]], ["pane", "tab"])
        self.assertEqual(saved["closed"][0]["pane_id"], "pA")
        self.assertEqual(saved["closed"][1]["tab_id"], "w1:t2")

    def test_stale_pane_entries_are_pruned_when_their_tab_dies(self):
        stale = {
            "kind": "pane",
            "pane_id": "pA",
            "terminal_id": "term_a",
            "tab_id": "w1:t1",
            "closed_ms": 1,
        }
        store.save_state(
            self.state_dir,
            {"version": 2, "index": {"w1:t1": two_pane_tab()}, "closed": [stale]},
        )
        api = self._live_tab_api([], None, tabs=[])
        record.run_record_pass(api, self.state_dir, max_entries=25)

        saved = store.load_state(self.state_dir)
        self.assertEqual([c["kind"] for c in saved["closed"]], ["tab"])
        self.assertEqual(saved["closed"][0]["tab_id"], "w1:t1")

    def test_pane_entries_are_pruned_for_a_forced_closed_tab_still_in_tab_list(self):
        # tab.closed usually arrives while the tab is still listed, so pruning
        # must follow the death, not the listing.
        stale = {"kind": "pane", "pane_id": "pA", "tab_id": "w1:t1", "closed_ms": 1}
        store.save_state(
            self.state_dir,
            {"version": 2, "index": {"w1:t1": two_pane_tab()}, "closed": [stale]},
        )
        api = self._live_tab_api(
            [{"pane_id": "pB", "terminal_id": "term_b"}], pane_node(pane_id="pB")
        )
        record.run_record_pass(api, self.state_dir, max_entries=25, forced_tab_id="w1:t1")

        saved = store.load_state(self.state_dir)
        self.assertEqual([c["kind"] for c in saved["closed"]], ["tab"])

    def test_an_empty_pane_list_is_not_read_as_every_pane_dying(self):
        # A live tab always holds at least one live pane, so an empty answer
        # is unusable rather than a mass death.
        store.save_state(
            self.state_dir,
            {"version": 2, "index": {"w1:t1": two_pane_tab()}, "closed": []},
        )
        api = self._live_tab_api([], pane_node(pane_id="pB", cwd="/proj/sub"))
        record.run_record_pass(api, self.state_dir, max_entries=25)
        self.assertEqual(store.load_state(self.state_dir)["closed"], [])

    def test_a_failing_pane_list_still_refreshes_the_tab_index(self):
        store.save_state(self.state_dir, {"version": 2, "index": {}, "closed": []})
        api = FakeApi(
            {
                "tab.list": {"tabs": [{"workspace_id": "w1", "tab_id": "w1:t1", "number": 1, "label": "probe"}]},
                "workspace.list": {"workspaces": [{"workspace_id": "w1", "label": "maestro"}]},
                "pane.list": RuntimeError("no such method"),
                "agent.list": {"agents": []},
                "layout.export": {"layout": {"root": pane_node(pane_id="pA", cwd="/proj")}},
            }
        )
        record.run_record_pass(api, self.state_dir, max_entries=25)
        saved = store.load_state(self.state_dir)
        self.assertIn("w1:t1", saved["index"])
        self.assertEqual(saved["closed"], [])

    def test_v1_state_file_upgrades_without_inventing_pane_deaths(self):
        v1_entry = {
            "tab_id": "w1:t1",
            "workspace_id": "w1",
            "workspace_label": "maestro",
            "label": "probe",
            "index": 0,
            "root": {"type": "pane", "cwd": "/proj"},
            "panes": [{"pane_id": "pA", "cwd": "/proj"}],
            "refreshed_ms": 1,
        }
        store.save_state(
            self.state_dir, {"version": 1, "index": {"w1:t1": v1_entry}, "closed": []}
        )
        api = self._live_tab_api([], pane_node(pane_id="pA", cwd="/proj"))
        record.run_record_pass(api, self.state_dir, max_entries=25)

        saved = store.load_state(self.state_dir)
        self.assertEqual(saved["closed"], [])
        self.assertEqual(saved["version"], 2)
        self.assertIn("tree", saved["index"]["w1:t1"])


if __name__ == "__main__":
    unittest.main()
