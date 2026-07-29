"""Tests for herdr_undo_close.record: tree helpers, live-index computation,
death reconciliation, and refresh. A fake API stands in for the socket --
no network, no real Herdr process involved."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from herdr_undo_close import record, store


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


def pane_node(pane_id=None, cwd=None):
    node = {"type": "pane"}
    if pane_id is not None:
        node["pane_id"] = pane_id
    if cwd is not None:
        node["cwd"] = cwd
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
    return {
        "tab_id": tab_id,
        "workspace_id": workspace_id,
        "workspace_label": "ws",
        "label": "probe",
        "index": 0,
        "root": {"type": "pane", "cwd": "/x"},
        "panes": [{"pane_id": "pA", "cwd": "/x"}],
        "refreshed_ms": refreshed_ms,
    }


class TestReconcileDeaths(unittest.TestCase):
    def test_tab_missing_from_live_moves_to_closed(self):
        state = {"index": {"w1:t1": make_record("w1:t1")}, "closed": []}
        dead = record.reconcile_deaths(state, live_ids={}, forced_tab_id=None, now_ms=999)
        self.assertEqual(dead, ["w1:t1"])
        self.assertNotIn("w1:t1", state["index"])
        self.assertEqual(len(state["closed"]), 1)
        self.assertEqual(state["closed"][0]["tab_id"], "w1:t1")
        self.assertEqual(state["closed"][0]["closed_ms"], 999)

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


class TestRefreshLive(unittest.TestCase):
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
        self.assertNotIn("pane_id", rec["root"])
        self.assertEqual(rec["panes"], [{"pane_id": "pA", "cwd": "/proj"}])

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


class TestRunRecordPass(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix="herdr-undo-close-test-")

    def tearDown(self):
        shutil.rmtree(self.state_dir, ignore_errors=True)

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


if __name__ == "__main__":
    unittest.main()
