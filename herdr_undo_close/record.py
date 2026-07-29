"""Reconcile live tabs against the state index and refresh their records.

This is the logic behind the `record` subcommand, which runs as both the
startup hook and the event hook for the events listed in herdr-plugin.toml.
One pass, under the state lock:

1. tab.list -> which tabs are alive right now, and their 0-based position
   among the tabs of their own workspace (ignoring the "number" field).
2. workspace.list -> label per workspace_id.
3. Deaths: any tab_id previously tracked in the index that is no longer
   live (or is the tab.closed event's forced tab_id) moves to the front of
   `closed`. This also covers the case where closing the last pane of a tab
   destroys the tab without ever emitting tab.closed: the next reconciled
   event notices the tab_id missing from tab.list and retires it.
4. Refresh: for each live tab, layout.export it and upsert a fresh record.
   A failed or empty export leaves the existing record untouched - a
   record must never be replaced by a worse one.
5. Trim and write.
"""

import time
from typing import Any, Dict, List, Optional

from . import store
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


def flatten_panes(node):
    # type: (Optional[Dict[str, Any]]) -> List[Dict[str, Any]]
    """Flatten a layout tree into pane dicts in tree order (first before
    second). Each entry keeps 'pane_id' (for display/debug only) and 'cwd'
    when present; agent info is attached later by enrich_with_agents."""
    panes = []  # type: List[Dict[str, Any]]
    _flatten_into(node, panes)
    return panes


def _flatten_into(node, out):
    # type: (Optional[Dict[str, Any]], List[Dict[str, Any]]) -> None
    if node is None:
        return
    node_type = node.get("type")
    if node_type == "pane":
        entry = {}
        if "pane_id" in node:
            entry["pane_id"] = node["pane_id"]
        if "cwd" in node:
            entry["cwd"] = node["cwd"]
        out.append(entry)
    elif node_type == "split":
        _flatten_into(node.get("first"), out)
        _flatten_into(node.get("second"), out)


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


def reconcile_deaths(state, live_ids, forced_tab_id, now_ms):
    # type: (Dict[str, Any], Dict[str, Dict[str, Any]], Optional[str], int) -> List[str]
    """Move every tracked tab_id that is missing from live_ids (or equal to
    forced_tab_id) to the front of `closed`, dropping any older `closed`
    entry for the same tab_id first so the stack never holds duplicates.
    Mutates state; returns the list of tab_ids that died in this pass."""
    index = state["index"]
    dead_ids = [
        tab_id
        for tab_id in list(index.keys())
        if tab_id not in live_ids or tab_id == forced_tab_id
    ]
    for tab_id in dead_ids:
        record = index.pop(tab_id)
        state["closed"] = [c for c in state["closed"] if c.get("tab_id") != tab_id]
        dying = dict(record)
        dying["closed_ms"] = now_ms
        state["closed"].insert(0, dying)
    return dead_ids


def refresh_live(state, api, live_ids, workspace_labels, agents, now_ms):
    # type: (Dict[str, Any], HerdrApi, Dict[str, Dict[str, Any]], Dict[str, str], List[Dict[str, Any]], int) -> None
    """For each live tab, layout.export it and upsert a fresh record. Leave
    the existing record alone when the export fails or has no pane nodes
    (a record must never be replaced by a worse one)."""
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

        enrich_with_agents(panes, agents)
        workspace_id = live["workspace_id"]
        state["index"][tab_id] = {
            "tab_id": tab_id,
            "workspace_id": workspace_id,
            "workspace_label": workspace_labels.get(workspace_id, ""),
            "label": live["label"],
            "index": live["index"],
            "root": strip_pane_ids(root),
            "panes": panes,
            "refreshed_ms": now_ms,
        }


def run_record_pass(api, state_dir, max_entries, forced_tab_id=None):
    # type: (HerdrApi, str, int, Optional[str]) -> Dict[str, Any]
    """Run one full record pass under the state lock and return the saved
    state (mainly useful for tests; callers normally ignore the return)."""
    now_ms = current_time_ms()
    with store.locked(state_dir):
        state = store.load_state(state_dir)

        tabs = api.call("tab.list", {}).get("tabs", [])
        live_ids = build_live_index(tabs)

        workspaces = api.call("workspace.list", {}).get("workspaces", [])
        workspace_labels = {w["workspace_id"]: w.get("label", "") for w in workspaces}

        dead_ids = set(reconcile_deaths(state, live_ids, forced_tab_id, now_ms))

        agents = api.call("agent.list", {}).get("agents", [])
        refresh_ids = {
            tab_id: live for tab_id, live in live_ids.items() if tab_id not in dead_ids
        }
        refresh_live(state, api, refresh_ids, workspace_labels, agents, now_ms)

        store.trim(state, max_entries)
        store.save_state(state_dir, state)

    return state
