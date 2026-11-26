"""Reconcile live tabs and panes against the state index and refresh records.

This is the logic behind the `record` subcommand, which runs as both the
startup hook and the event hook for the events listed in herdr-plugin.toml.
One pass, under the state lock:

1. tab.list -> which tabs are alive right now, and their 0-based position
   among the tabs of their own workspace (ignoring the "number" field).
2. workspace.list -> label per workspace_id.
3. pane.list {} (no params = every workspace) -> the set of live pane ids
   and the set of live terminal ids. Both are needed, see below.
4. agent.list -> which panes are running an agent.
5. Tab deaths: any tab_id previously tracked in the index that is no longer
   live (or is the tab.closed event's forced tab_id) moves to the front of
   `closed` as a kind == "tab" entry. This also covers the case where
   closing the last pane of a tab destroys the tab without ever emitting
   tab.closed: the next reconciled event notices the tab_id missing from
   tab.list and retires it. Then every kind == "pane" entry whose tab is no
   longer live is pruned - its tab is gone, so it can never be restored,
   and leaving it in would park dead weight at the front of the stack.
6. Pane deaths: for each still-live tracked tab, any pane in its PREVIOUS
   record that has died becomes a kind == "pane" entry.

   A pane died iff its pane_id is absent from pane.list AND its recorded
   terminal_id is also absent. The two-part test is not belt and braces:
   moving a pane to another workspace changes its pane_id while the
   terminal_id follows the pane, so an id-only test would record every
   cross-workspace move as a bogus undo entry. Closing a pane for real
   removes both.
7. Refresh: for each live tab, layout.export it and upsert a fresh record.
   A failed or empty export leaves the existing record untouched - a
   record must never be replaced by a worse one. Each live pane is also
   asked what it is running (one pane.process_info, or nothing at all for
   a pane whose agent.list entry already answers it), so a restore can
   bring the process back and not just the layout. That question is only
   answerable while the pane lives, which is why it is asked every pass
   and not at death.
8. Trim and write.

Steps 5 and 6 both stamp the same now_ms, so the stack cannot order them by
time. The tie-break is insertion order: tab deaths are inserted first and
pane deaths second, each at position 0, which leaves pane deaths ahead of
tab deaths for a pass that saw both. That is deliberate and there is no
sort - the only way to see both in one pass is a tab dying while an
unrelated pane died, and the pane is the more surprising loss to offer
first.

The index keeps each tab's layout tree UNSTRIPPED, under the key "tree",
because a pane-death record is computed from it and needs pane ids to find
the dead pane's parent split. The safety property that mattered in version 1
is preserved where it matters: a dying tab's record stores the STRIPPED tree
under "root", so a stale pane id can never be read back by layout.apply as
"move this live pane".
"""

import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from . import relaunch, store
from .api import HerdrApi


def current_time_ms():
    # type: () -> int
    return int(time.time() * 1000)


def strip_pane_ids(node):
    # type: (Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]
    """Return a deep copy of a layout tree node with 'pane_id' removed from
    every pane node, so a stale id can never be read back as "move this
    pane". Split nodes are recursed into via 'first'/'second'."""
    if node is None:
        return None
    node_type = node.get("type")
    if node_type == "pane":
        return {k: v for k, v in node.items() if k != "pane_id"}
    if node_type == "split":
        new_node = dict(node)
        new_node["first"] = strip_pane_ids(node.get("first"))
        new_node["second"] = strip_pane_ids(node.get("second"))
        return new_node
    # Unknown node shape: copy defensively rather than dropping it.
    return dict(node)


def flatten_pane_nodes(node):
    # type: (Optional[Dict[str, Any]]) -> List[Dict[str, Any]]
    """The pane NODES themselves in tree order (first before second).

    These are the live dicts out of the tree, not copies: the tab restore
    walks them to hang a `command` on the right ones. Callers that do not
    intend to mutate the tree want flatten_panes instead.
    """
    nodes = []  # type: List[Dict[str, Any]]
    _flatten_into(node, nodes)
    return nodes


def flatten_panes(node):
    # type: (Optional[Dict[str, Any]]) -> List[Dict[str, Any]]
    """Flatten a layout tree into pane dicts in tree order (first before
    second). Each entry keeps 'pane_id' (for display/debug only) and 'cwd'
    when present; agent info is attached later by enrich_with_agents.

    Built on flatten_pane_nodes so the two are the same order BY
    CONSTRUCTION, not by coincidence. That invariant is load-bearing: a
    record's `panes` list is this function's output, and the tab restore
    zips it positionally against flatten_pane_nodes of the stored tree to
    decide which node gets which relaunch command.
    """
    panes = []  # type: List[Dict[str, Any]]
    for node_ in flatten_pane_nodes(node):
        entry = {}
        if "pane_id" in node_:
            entry["pane_id"] = node_["pane_id"]
        if "cwd" in node_:
            entry["cwd"] = node_["cwd"]
        panes.append(entry)
    return panes


def _flatten_into(node, out):
    # type: (Optional[Dict[str, Any]], List[Dict[str, Any]]) -> None
    if not isinstance(node, dict):
        return
    node_type = node.get("type")
    if node_type == "pane":
        out.append(node)
    elif node_type == "split":
        _flatten_into(node.get("first"), out)
        _flatten_into(node.get("second"), out)


def find_pane_node(node, pane_id, parent=None, side=None):
    # type: (Optional[Dict[str, Any]], str, Optional[Dict[str, Any]], Optional[str]) -> Optional[Tuple[Dict[str, Any], Optional[Dict[str, Any]], Optional[str]]]
    """Locate a pane in a layout tree by id.

    Returns (pane_node, parent_split_node, side) where side is "first" or
    "second", or None when the id is not in the tree. parent is None for the
    root pane of a single-pane tab - a pane with no parent split cannot be
    restored by splitting a sibling, and in practice its tab died with it.
    """
    if not isinstance(node, dict):
        return None
    node_type = node.get("type")
    if node_type == "pane":
        if node.get("pane_id") == pane_id:
            return node, parent, side
        return None
    if node_type == "split":
        found = find_pane_node(node.get("first"), pane_id, node, "first")
        if found is not None:
            return found
        return find_pane_node(node.get("second"), pane_id, node, "second")
    return None


def sibling_pane_ids(sibling, dead_side):
    # type: (Optional[Dict[str, Any]], Optional[str]) -> List[str]
    """The sibling subtree's leaf pane ids, ordered adjacent-first.

    When a pane dies its parent split collapses and the sibling subtree takes
    the parent's place, so restoring means splitting one of the sibling's
    leaves. Index 0 must therefore be the leaf that physically touched the
    dead pane: if the dead pane was `first`, that is the sibling's first leaf
    in tree order; if it was `second`, it is the sibling's last leaf, so the
    list is reversed. The restore then just walks the list for the first id
    that is still alive.
    """
    ids = [p["pane_id"] for p in flatten_panes(sibling) if p.get("pane_id")]
    if dead_side == "second":
        ids.reverse()
    return ids


def build_pane_record(tab_record, pane_entry, tree, now_ms):
    # type: (Dict[str, Any], Dict[str, Any], Optional[Dict[str, Any]], int) -> Optional[Dict[str, Any]]
    """Build a kind == "pane" closed entry from the tab's PREVIOUS tree.

    Returns None when the pane cannot be located in that tree, or when it had
    no parent split (a single-pane tab: the tab itself died, and the tab entry
    already covers it). Everything the restore needs comes from the recorded
    tree plus the recorded pane entry, because by the time this runs the pane
    is gone from Herdr and cannot be queried.
    """
    pane_id = pane_entry.get("pane_id")
    if not pane_id:
        return None
    found = find_pane_node(tree, pane_id)
    if found is None:
        return None
    node, parent, side = found
    if parent is None:
        return None

    sibling = parent.get("second") if side == "first" else parent.get("first")
    entry = {
        "kind": "pane",
        "pane_id": pane_id,
        "terminal_id": pane_entry.get("terminal_id"),
        "tab_id": tab_record.get("tab_id"),
        "workspace_id": tab_record.get("workspace_id"),
        "workspace_label": tab_record.get("workspace_label", ""),
        "tab_label": tab_record.get("label", ""),
        "cwd": node.get("cwd", pane_entry.get("cwd")),
        "label": node.get("label", pane_entry.get("label")),
        "env": node.get("env"),
        "command": node.get("command"),
        "parent": {
            "direction": parent.get("direction"),
            "ratio": parent.get("ratio"),
            "dead_side": side,
            "sibling_pane_ids": sibling_pane_ids(sibling, side),
            "sibling_is_leaf": isinstance(sibling, dict) and sibling.get("type") == "pane",
        },
        "closed_ms": now_ms,
    }  # type: Dict[str, Any]
    agent = pane_entry.get("agent")
    if agent:
        entry["agent"] = agent
    # What the pane was running, worked out while it was still alive. The
    # pane is gone by the time this runs, so it is now or never.
    relaunch_intent = pane_entry.get("relaunch")
    if relaunch_intent:
        entry["relaunch"] = relaunch_intent
    return entry


def enrich_with_terminals(panes, panes_by_id):
    # type: (List[Dict[str, Any]], Dict[str, Dict[str, Any]]) -> None
    """Attach terminal_id (and label, and cwd when the tree had none) to each
    pane from a single pane.list result. terminal_id is what makes the death
    test able to tell a closed pane apart from a moved one, so a pane without
    it is deliberately never treated as dead. Mutates panes."""
    for entry in panes:
        info = panes_by_id.get(entry.get("pane_id"))
        if not info:
            continue
        if info.get("terminal_id"):
            entry["terminal_id"] = info["terminal_id"]
        if info.get("label"):
            entry["label"] = info["label"]
        if not entry.get("cwd") and info.get("cwd"):
            entry["cwd"] = info["cwd"]


def enrich_with_agents(panes, agents):
    # type: (List[Dict[str, Any]], List[Dict[str, Any]]) -> None
    """Attach agent/agent_status/agent_session to panes that have a live
    agent (from a single agent.list call). Keys are omitted entirely when
    the pane had no agent, matching the state schema. Mutates panes."""
    by_pane = {}
    for agent in agents:
        pane_id = agent.get("pane_id")
        if pane_id is not None:
            by_pane[pane_id] = agent

    for entry in panes:
        agent = by_pane.get(entry.get("pane_id"))
        if agent is None:
            continue
        if "agent" in agent:
            entry["agent"] = agent["agent"]
        if "agent_status" in agent:
            entry["agent_status"] = agent["agent_status"]
        if "agent_session" in agent:
            entry["agent_session"] = agent["agent_session"]


def keep_owned_claude_sessions(panes, previous_panes):
    # type: (List[Dict[str, Any]], List[Dict[str, Any]]) -> None
    """Keep a claude pane's own session when Herdr reports another one.

    Herdr can adopt the session of a nested `claude -p` run as the pane's
    session (see relaunch.claude_session_in_cwd), and reopening the pane
    would then resume that run instead. When the reported session has no
    log under the pane's cwd, keep the one recorded for the pane last pass
    if that one does; otherwise drop the report so the relaunch falls back
    to the cwd lookup. Mutates panes."""
    before = dict((p.get("pane_id"), p) for p in previous_panes)
    for entry in panes:
        if entry.get("agent") != "claude":
            continue
        cwd = entry.get("cwd")
        if relaunch.claude_session_in_cwd(relaunch.reported_session_id(entry), cwd):
            continue
        if "agent_session" not in entry:
            continue
        prev = before.get(entry.get("pane_id")) or {}
        if prev.get("agent") == "claude" and relaunch.claude_session_in_cwd(
            relaunch.reported_session_id(prev), cwd
        ):
            entry["agent_session"] = prev["agent_session"]
        else:
            del entry["agent_session"]


def build_live_index(tabs):
    # type: (List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]
    """tabs: tab.list's "tabs" array, already in per-workspace display
    order. Returns tab_id -> {workspace_id, label, index} where index is
    the 0-based position among tabs sharing that workspace_id (the
    "number" field is not the display position and is ignored)."""
    live = {}
    counters = {}  # type: Dict[str, int]
    for tab in tabs:
        workspace_id = tab["workspace_id"]
        position = counters.get(workspace_id, 0)
        counters[workspace_id] = position + 1
        live[tab["tab_id"]] = {
            "workspace_id": workspace_id,
            "label": tab.get("label", ""),
            "index": position,
        }
    return live


def build_tab_closed_entry(record, now_ms):
    # type: (Dict[str, Any], int) -> Dict[str, Any]
    """Turn a live index record into a kind == "tab" closed entry.

    The index holds the tree unstripped under "tree"; the closed entry holds
    it STRIPPED under "root", which is the shape restore_record and
    build_apply_params already consume. A version 1 index record has no
    "tree" and its "root" is already stripped, so it is left as it is.
    """
    dying = dict(record)
    tree = dying.pop("tree", None)
    if tree is not None:
        dying["root"] = strip_pane_ids(tree)
    dying["kind"] = "tab"
    dying["closed_ms"] = now_ms
    return dying


def reconcile_deaths(state, live_ids, forced_tab_id, now_ms):
    # type: (Dict[str, Any], Dict[str, Dict[str, Any]], Optional[str], int) -> List[str]
    """Move every tracked tab_id that is missing from live_ids (or equal to
    forced_tab_id) to the front of `closed`, dropping any older kind == "tab"
    entry for the same tab_id first so the stack never holds duplicates.
    Pane entries for that tab are left to prune_orphan_pane_entries, which
    knows why they have to go. Mutates state; returns the tab_ids that died
    in this pass."""
    index = state["index"]
    dead_ids = [
        tab_id
        for tab_id in list(index.keys())
        if tab_id not in live_ids or tab_id == forced_tab_id
    ]
    for tab_id in dead_ids:
        record = index.pop(tab_id)
        state["closed"] = [
            c
            for c in state["closed"]
            if not (store.entry_kind(c) == "tab" and c.get("tab_id") == tab_id)
        ]
        state["closed"].insert(0, build_tab_closed_entry(record, now_ms))
    return dead_ids


def prune_orphan_pane_entries(state, live_ids):
    # type: (Dict[str, Any], Dict[str, Dict[str, Any]]) -> int
    """Drop every kind == "pane" entry whose tab is no longer live.

    A pane restore splits a surviving pane of the pane's original tab, so once
    that tab is gone the entry is unrestorable forever. Keeping it would park
    a permanently failing entry at the front of the stack, where "reopen last"
    would hit it first. Returns how many entries were dropped.
    """
    closed = state.get("closed", [])
    kept = [
        c
        for c in closed
        if not (store.entry_kind(c) == "pane" and c.get("tab_id") not in live_ids)
    ]
    state["closed"] = kept
    return len(closed) - len(kept)


def reconcile_pane_deaths(state, live_ids, live_pane_ids, live_terminal_ids, now_ms):
    # type: (Dict[str, Any], Dict[str, Dict[str, Any]], Set[str], Set[str], int) -> List[Dict[str, Any]]
    """Record a closed-pane entry for every pane of a still-live tracked tab
    that has died since the last pass, newest first.

    Must run BEFORE refresh_live: the record is built from the tab's previous
    tree, which is the only place the dead pane still exists. A pane is
    skipped when the previous record has no terminal_id for it (a version 1
    state file, or a pane.list that did not report one): without it the
    moved-versus-closed test is impossible, and a bogus undo entry is worse
    than a missed one. The very next pass has the field, so the blind window
    is a single event.
    """
    entries = []  # type: List[Dict[str, Any]]
    for tab_id, tab_record in list(state.get("index", {}).items()):
        if tab_id not in live_ids:
            # Died in this same pass: the tab entry already covers its panes.
            continue
        tree = tab_record.get("tree")
        if not isinstance(tree, dict):
            continue
        for pane_entry in tab_record.get("panes", []):
            pane_id = pane_entry.get("pane_id")
            terminal_id = pane_entry.get("terminal_id")
            if not pane_id or not terminal_id:
                continue
            if pane_id in live_pane_ids or terminal_id in live_terminal_ids:
                continue
            built = build_pane_record(tab_record, pane_entry, tree, now_ms)
            if built is None:
                continue
            entries.append(built)

    for entry in entries:
        # A refresh that failed in the previous pass leaves the dead pane in
        # the tab's record, so the same death can be seen twice. Drop the
        # older entry for that pane id the way tab deaths drop theirs.
        pane_id = entry["pane_id"]
        state["closed"] = [
            c
            for c in state["closed"]
            if not (store.entry_kind(c) == "pane" and c.get("pane_id") == pane_id)
        ]
        state["closed"].insert(0, entry)
    return entries


def refresh_live(
    state, api, live_ids, workspace_labels, agents, now_ms, panes_by_id=None, restore_process=True
):
    # type: (Dict[str, Any], HerdrApi, Dict[str, Dict[str, Any]], Dict[str, str], List[Dict[str, Any]], int, Optional[Dict[str, Dict[str, Any]]], bool) -> None
    """For each live tab, layout.export it and upsert a fresh record. Leave
    the existing record alone when the export fails or has no pane nodes
    (a record must never be replaced by a worse one).

    "tree" is the exported root kept as it came, pane ids included, because
    the next pass needs them to build a pane-death record. Stripping happens
    later, when a tab dies.

    The tree is NOT rewritten with relaunch intents: they live on the
    `panes` entries, which are the same order as the tree's pane nodes by
    construction (see flatten_panes), so a restore can zip the two.
    """
    previous_panes = [p for rec in state["index"].values() for p in rec.get("panes") or []]
    for tab_id, live in live_ids.items():
        try:
            result = api.call("layout.export", {"tab_id": tab_id})
        except Exception:
            continue

        layout = result.get("layout") if isinstance(result, dict) else None
        if not isinstance(layout, dict):
            continue

        root = layout.get("root")
        if not root:
            continue

        panes = flatten_panes(root)
        if not panes:
            continue

        enrich_with_terminals(panes, panes_by_id or {})
        enrich_with_agents(panes, agents)
        keep_owned_claude_sessions(panes, previous_panes)
        if restore_process:
            # Last, because an intent is decided from the agent and the cwd
            # the two enrichers above just filled in.
            relaunch.attach_intents(api, panes)
        workspace_id = live["workspace_id"]
        state["index"][tab_id] = {
            "tab_id": tab_id,
            "workspace_id": workspace_id,
            "workspace_label": workspace_labels.get(workspace_id, ""),
            "label": live["label"],
            "index": live["index"],
            "tree": root,
            "panes": panes,
            "refreshed_ms": now_ms,
        }


def run_record_pass(
    api, state_dir, max_entries, forced_tab_id=None, undo_panes=True, restore_process=True
):
    # type: (HerdrApi, str, int, Optional[str], bool, bool) -> Dict[str, Any]
    """Run one full record pass under the state lock and return the saved
    state (mainly useful for tests; callers normally ignore the return).

    With undo_panes false, pane deaths are not recorded at all and the plugin
    behaves like version 1: closing a pane only reshapes its tab's record.
    With restore_process false, no pane is asked what it is running, so a
    restore brings back the layout and nothing else.
    """
    now_ms = current_time_ms()
    with store.locked(state_dir):
        state = store.load_state(state_dir)

        tabs = api.call("tab.list", {}).get("tabs", [])
        live_ids = build_live_index(tabs)

        workspaces = api.call("workspace.list", {}).get("workspaces", [])
        workspace_labels = {w["workspace_id"]: w.get("label", "") for w in workspaces}

        # No params: every workspace, not just the hook's own.
        try:
            live_panes = api.call("pane.list", {}).get("panes", [])
        except Exception as exc:
            # Never let this cost us the tab index: without pane.list we
            # cannot tell a closed pane from a moved one, so we skip pane
            # deaths and still refresh every live tab below.
            sys.stderr.write("herdr-undo-close: pane.list failed (%s), skipping pane deaths\n" % exc)
            live_panes = []

        panes_by_id = {}  # type: Dict[str, Dict[str, Any]]
        live_pane_ids = set()
        live_terminal_ids = set()
        for pane in live_panes:
            pane_id = pane.get("pane_id")
            if pane_id:
                panes_by_id[pane_id] = pane
                live_pane_ids.add(pane_id)
            terminal_id = pane.get("terminal_id")
            if terminal_id:
                live_terminal_ids.add(terminal_id)

        agents = api.call("agent.list", {}).get("agents", [])

        dead_ids = set(reconcile_deaths(state, live_ids, forced_tab_id, now_ms))
        # A tab.closed event's tab is usually still in tab.list, so "surviving"
        # is not the same set as "listed": everything that just died has to be
        # excluded from the pruning, the pane scan and the refresh alike.
        surviving = {
            tab_id: live for tab_id, live in live_ids.items() if tab_id not in dead_ids
        }

        prune_orphan_pane_entries(state, surviving)

        # An empty pane list while tabs are alive is a contradiction (a live
        # tab always holds at least one live pane), so it means the answer was
        # unusable, not that every pane died. Believing it would flood the
        # stack with phantom entries.
        panes_known = bool(live_pane_ids) or not surviving
        if undo_panes and panes_known:
            reconcile_pane_deaths(
                state, surviving, live_pane_ids, live_terminal_ids, now_ms
            )

        refresh_live(
            state,
            api,
            surviving,
            workspace_labels,
            agents,
            now_ms,
            panes_by_id,
            restore_process=restore_process,
        )

        state["version"] = store.STATE_VERSION
        store.trim(state, max_entries)
        store.save_state(state_dir, state)

    return state
