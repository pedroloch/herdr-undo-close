"""Restore one closed-tab record: layout.apply plus a best-effort tab.move.

`reopen_at` (used by both the `reopen` subcommand and the picker) pops a
record from `closed`, restores it, and only removes it from the stack after
the restore succeeds:

    read under lock -> release -> restore (socket round trip) -> re-acquire
    to remove by (tab_id, closed_ms) -> release

The lock is never held across the layout.apply round trip, and removal is
keyed on (tab_id, closed_ms) rather than "pop index 0 again" so a concurrent
record pass that pushed a new death in between cannot cause the wrong entry
to be dropped.

What comes back is documented, not silently lossy: panes are plain shells
(no agent is restarted), and a workspace or position fallback is reported
so the caller can tell the user.
"""

import copy
from typing import Any, Dict, List, Optional, Tuple

from . import store
from .api import HerdrApi
from .record import strip_pane_ids


def clamp(value, low, high):
    # type: (int, int, int) -> int
    return max(low, min(value, high))


def choose_workspace(record, workspaces, env_workspace_id):
    # type: (Dict[str, Any], List[Dict[str, Any]], Optional[str]) -> Tuple[str, bool]
    """Pick the target workspace for a restore.

    Prefers the record's original workspace if it still exists. Otherwise
    falls back to the caller's current workspace (HERDR_WORKSPACE_ID) if
    that is itself still live, then to whichever workspace.list marks
    focused, then to the first workspace returned. Returns
    (workspace_id, fallback_used).
    """
    live_ids = set()
    for w in workspaces:
        wid = w.get("workspace_id")
        if wid is not None:
            live_ids.add(wid)

    recorded = record.get("workspace_id")
    if recorded in live_ids:
        return recorded, False

    if env_workspace_id and env_workspace_id in live_ids:
        return env_workspace_id, True

    for w in workspaces:
        if w.get("focused"):
            return w["workspace_id"], True

    if workspaces:
        return workspaces[0]["workspace_id"], True

    raise ValueError("no workspace available to restore into")


def build_apply_params(record, workspace_id, focus):
    # type: (Dict[str, Any], str, bool) -> Dict[str, Any]
    """layout.apply params. Strips pane_id from root again defensively (it
    is already stripped in the stored record) - a live id must never be
    interpreted as "move this pane". tab_id is deliberately omitted: that
    variant of layout.apply replaces an existing tab instead of creating
    a new one."""
    return {
        "workspace_id": workspace_id,
        "tab_label": record.get("label", ""),
        "focus": focus,
        "root": strip_pane_ids(record.get("root")),
    }


def summarize_agents(record):
    # type: (Dict[str, Any]) -> List[str]
    """Distinct agent kinds that were running on this tab's panes; a
    restore never restarts them, so callers report this as a caveat."""
    kinds = set()
    for pane in record.get("panes", []):
        agent = pane.get("agent")
        if agent:
            kinds.add(agent)
    return sorted(kinds)


def restore_record(api, record, workspaces, env_workspace_id, focus):
    # type: (HerdrApi, Dict[str, Any], List[Dict[str, Any]], Optional[str], bool) -> Dict[str, Any]
    """Apply a record's layout as a new tab, then best-effort reposition it.
    Returns a result summary; raises on a failed layout.apply (that failure
    must abort the pop, see reopen_at)."""
    workspace_id, fallback_used = choose_workspace(record, workspaces, env_workspace_id)
    params = build_apply_params(record, workspace_id, focus)
    result = api.call("layout.apply", params)

    layout = result.get("layout") if isinstance(result, dict) else None
    new_tab_id = None
    if isinstance(layout, dict):
        new_tab_id = layout.get("tab_id")
    if not new_tab_id and isinstance(result, dict):
        new_tab_id = result.get("tab_id")
    if not new_tab_id:
        raise ValueError("layout.apply did not return a tab_id")

    position_restored = False
    try:
        tabs = api.call("tab.list", {}).get("tabs", [])
        n_tabs = sum(1 for t in tabs if t.get("workspace_id") == workspace_id)
        insert_index = clamp(record.get("index", 0), 0, max(n_tabs - 1, 0))
        api.call("tab.move", {"tab_id": new_tab_id, "insert_index": insert_index})
        position_restored = True
    except Exception:
        position_restored = False

    return {
        "new_tab_id": new_tab_id,
        "workspace_id": workspace_id,
        "fallback_used": fallback_used,
        "position_restored": position_restored,
        "pane_count": len(record.get("panes", [])),
        "agent_kinds": summarize_agents(record),
        "label": record.get("label", ""),
    }


def reopen_at(api, state_dir, config, env_workspace_id, index=0):
    # type: (HerdrApi, str, Dict[str, Any], Optional[str], int) -> Optional[Dict[str, Any]]
    """Restore the closed record at `index` (0 is the most recently closed
    tab). Returns None when there is nothing at that index. The entry is
    only removed from `closed` once the restore has actually succeeded."""
    with store.locked(state_dir):
        state = store.load_state(state_dir)
        closed = state.get("closed", [])
        if index >= len(closed):
            return None
        record = copy.deepcopy(closed[index])

    workspaces = api.call("workspace.list", {}).get("workspaces", [])
    result = restore_record(api, record, workspaces, env_workspace_id, config["focus"])

    with store.locked(state_dir):
        state = store.load_state(state_dir)
        state["closed"] = [
            c
            for c in state.get("closed", [])
            if not (
                c.get("tab_id") == record.get("tab_id")
                and c.get("closed_ms") == record.get("closed_ms")
            )
        ]
        store.save_state(state_dir, state)

    return result


def reopen_last(api, state_dir, config, env_workspace_id):
    # type: (HerdrApi, str, Dict[str, Any], Optional[str]) -> Optional[Dict[str, Any]]
    """Restore the single most recently closed tab (action reopen-last)."""
    return reopen_at(api, state_dir, config, env_workspace_id, index=0)
