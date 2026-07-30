"""Restore one closed entry: a whole tab, or a single pane into a live tab.

`reopen_at` (used by both the `reopen` subcommand and the picker) reads an
entry from `closed`, restores it, and only removes it from the stack after
the restore succeeds:

    read under lock -> release -> restore (socket round trips) -> re-acquire
    to remove by identity -> release

The lock is never held across a socket round trip, and removal is keyed on
identity - (tab_id, closed_ms) for a tab, (pane_id, closed_ms) for a pane -
rather than "pop index 0 again", so a concurrent record pass that pushed a
new death in between cannot cause the wrong entry to be dropped.

Two restore paths, because Herdr gives us two very different tools:

* A closed TAB is rebuilt with layout.apply WITHOUT a tab_id, which creates a
  new tab, plus a best-effort tab.move. This path replays per-pane launch
  commands, because layout.apply's tree carries them, and it is also how a
  recorded process comes back: a `command` on a pane node is honoured, so the
  restore hangs one there.
* A closed PANE is rebuilt with pane.split + pane.rename + pane.swap on the
  tab that is still alive. It cannot use layout.apply: measured against Herdr
  0.7.5, layout.apply WITH a tab_id closes the target tab, returns a
  different tab_id, mints all-new pane ids and loses the surviving panes'
  scrollback. A pane_id inside a LayoutNode does not mean "reuse this live
  pane", so there is no non-destructive layout.apply for a surviving tab.
  pane.split has no `command` parameter either, so bringing a process back
  here means typing it into the new pane's shell with pane.send_text, once
  that shell is provably ready. The dead pane's original launch command is
  still not replayed, and the summary says so when nothing took its place.

What comes back is documented, not silently lossy: a workspace or position
fallback is reported, an approximate split position is reported, a dropped
launch command is reported, and every process is reported as relaunched,
deliberately skipped, or attempted and failed. Caveats that stopped being
true are dropped rather than inverted: an agent that WAS resumed is not
also announced as "not restarted".
"""

import copy
import sys
from typing import Any, Dict, List, Optional, Tuple

from . import relaunch as relaunch_mod
from . import store
from .api import HerdrApi
from .record import flatten_pane_nodes, flatten_panes, strip_pane_ids


class PaneTabGone(Exception):
    """A closed-pane entry was restored but its tab is no longer alive.

    Distinguishable on purpose: the caller reports "its tab is gone" (and the
    next record pass prunes the entry) instead of a generic failure, which
    would read as a bug in the plugin.
    """


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


def tab_relaunch_plans(record, config):
    # type: (Dict[str, Any], Dict[str, Any]) -> List[Optional[Dict[str, Any]]]
    """One relaunch decision per pane, positionally aligned with the
    record's `panes` list (None where there is nothing to say). Pure, so it
    can be called once to rewrite the tree and once to build the summary
    without the two ever disagreeing."""
    return [
        relaunch_mod.relaunch_plan(pane.get("relaunch"), config)
        for pane in record.get("panes", [])
    ]


def build_apply_params(record, workspace_id, focus, config):
    # type: (Dict[str, Any], str, bool, Dict[str, Any]) -> Dict[str, Any]
    """layout.apply params. Strips pane_id from root again defensively (it
    is already stripped in the stored record) - a live id must never be
    interpreted as "move this pane". tab_id is deliberately omitted: that
    variant of layout.apply replaces an existing tab instead of creating
    a new one.

    This is also where a recorded process comes back: layout.apply honours a
    `command` array on a pane node, so an allowed relaunch is hung on the
    node the pane came from. The pairing is positional - a record's `panes`
    list is flatten_panes of its own tree, so it is the same order as
    flatten_pane_nodes of the tree stored beside it. Nodes without an
    allowed relaunch are left alone and come back as plain shells.
    """
    root = strip_pane_ids(record.get("root"))
    nodes = flatten_pane_nodes(root)
    for node, plan in zip(nodes, tab_relaunch_plans(record, config)):
        if plan and plan.get("allowed") and plan.get("command"):
            node["command"] = list(plan["command"])
    return {
        "workspace_id": workspace_id,
        "tab_label": record.get("label", ""),
        "focus": focus,
        "root": root,
    }


def summarize_agents(record, plans=None):
    # type: (Dict[str, Any], Optional[List[Optional[Dict[str, Any]]]]) -> List[str]
    """Distinct agent kinds this restore did NOT bring back, which callers
    report as a caveat. An agent whose conversation was resumed is left out:
    saying "claude was not restarted" about a pane that is running claude
    again would be a lie in the opposite direction."""
    resumed = set()
    for plan in plans or []:
        if plan and plan.get("kind") == "agent" and plan.get("allowed"):
            resumed.add(plan.get("program"))
    kinds = set()
    for pane in record.get("panes", []):
        agent = pane.get("agent")
        if agent and agent not in resumed:
            kinds.add(agent)
    return sorted(kinds)


def restore_record(api, record, workspaces, env_workspace_id, focus, config):
    # type: (HerdrApi, Dict[str, Any], List[Dict[str, Any]], Optional[str], bool, Dict[str, Any]) -> Dict[str, Any]
    """Apply a record's layout as a new tab, then best-effort reposition it.
    Returns a result summary; raises on a failed layout.apply (that failure
    must abort the pop, see reopen_at)."""
    workspace_id, fallback_used = choose_workspace(record, workspaces, env_workspace_id)
    plans = tab_relaunch_plans(record, config)
    params = build_apply_params(record, workspace_id, focus, config)
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
        "kind": "tab",
        "new_tab_id": new_tab_id,
        "workspace_id": workspace_id,
        "fallback_used": fallback_used,
        "position_restored": position_restored,
        "pane_count": len(record.get("panes", [])),
        "agent_kinds": summarize_agents(record, plans),
        "label": record.get("label", ""),
        # layout.apply either created the whole tab or raised, so there is no
        # third "tried and failed" outcome to report on this path.
        "relaunched": [p for p in plans if p and p.get("allowed")],
        "not_relaunched": [p for p in plans if p and not p.get("allowed")],
        "relaunch_failed": [],
    }


def live_leaf_pane_ids(api, tab_id):
    # type: (HerdrApi, str) -> List[str]
    """The pane ids currently living in a tab, in tree order."""
    result = api.call("layout.export", {"tab_id": tab_id})
    layout = result.get("layout") if isinstance(result, dict) else None
    root = layout.get("root") if isinstance(layout, dict) else None
    return [p["pane_id"] for p in flatten_panes(root) if p.get("pane_id")]


def choose_anchor(recorded_ids, live_ids):
    # type: (List[str], List[str]) -> Tuple[Optional[str], bool]
    """Pick the live pane to split, and say whether it is the recorded one.

    recorded_ids is parent.sibling_pane_ids, already ordered adjacent-first,
    so the first entry that is still alive is the closest surviving neighbour
    of the dead pane. When none of them survived, any live pane of the tab
    still gets the pane back into the right tab, which is the point - the
    caller reports the position as approximate. Returns (anchor, is_recorded)
    where is_recorded is true only for recorded_ids[0], the exact neighbour.
    """
    live = set(live_ids)
    for candidate in recorded_ids:
        if candidate in live:
            return candidate, candidate == recorded_ids[0]
    return (live_ids[0] if live_ids else None), False


def run_pane_relaunch(api, pane_id, plan):
    # type: (HerdrApi, str, Optional[Dict[str, Any]]) -> bool
    """Type an allowed relaunch into a freshly restored pane. Returns
    whether it actually went in.

    Never raises: by the time this runs the pane is back, with its cwd, its
    label and its place in the split, and throwing that away because a
    program could not be started would turn a good restore into a failed
    one. A refusal is reported by the caller instead, which is the whole
    point of returning a bool.
    """
    if not plan or not plan.get("allowed") or not plan.get("command"):
        return False
    try:
        if not relaunch_mod.wait_for_shell(api, pane_id):
            sys.stderr.write(
                "herdr-undo-close: pane %s never became ready, not relaunching %s\n"
                % (pane_id, plan.get("program"))
            )
            return False
        relaunch_mod.send_command(api, pane_id, plan["command"])
        return True
    except Exception as exc:
        sys.stderr.write(
            "herdr-undo-close: could not relaunch %s in %s (%s)\n"
            % (plan.get("program"), pane_id, exc)
        )
        return False


def restore_pane_record(api, record, config):
    # type: (HerdrApi, Dict[str, Any], Dict[str, Any]) -> Dict[str, Any]
    """Split a pane back into its original tab: pane.split, then pane.rename,
    then pane.swap when the dead pane was on the `first` side.

    pane.split always places the new pane as `second` of the split node it
    creates, and `ratio` is the share of `first`, stored verbatim. pane.swap
    exchanges two panes' positions and preserves the split node's ratio. So
    the same ratio is correct in both cases: for a `second` death the split
    alone reproduces the old node, and for a `first` death the swap moves the
    new pane to the `first` side without touching the ratio.

    Finally, when the pane was running something worth bringing back, the
    command is typed into the new pane once its shell is provably ready.
    That step is best effort by design: it happens after the pane exists, so
    it can only ever add to a restore that already succeeded.

    Raises PaneTabGone when the tab is gone, and ValueError when the tab has
    no pane to split from or pane.split returns no id. Both abort before the
    entry is removed from the stack (see reopen_at).
    """
    tab_id = record.get("tab_id")
    tabs = api.call("tab.list", {}).get("tabs", [])
    live_tab_ids = set()
    for tab in tabs:
        if tab.get("tab_id") is not None:
            live_tab_ids.add(tab["tab_id"])
    if tab_id not in live_tab_ids:
        raise PaneTabGone("tab %s is no longer open" % tab_id)

    live_ids = live_leaf_pane_ids(api, tab_id)
    if not live_ids:
        raise ValueError("tab %s has no pane to split from" % tab_id)

    parent = record.get("parent")
    if not isinstance(parent, dict):
        parent = {}
    recorded_ids = [i for i in (parent.get("sibling_pane_ids") or []) if i]
    anchor, is_recorded = choose_anchor(recorded_ids, live_ids)
    if anchor is None:
        raise ValueError("tab %s has no pane to split from" % tab_id)

    # Exact only when the sibling was a single pane AND that very pane is the
    # one being split: a subtree sibling means the dead pane's split node sat
    # above a nesting level that pane.split cannot reproduce in one step.
    exact = bool(parent.get("sibling_is_leaf")) and is_recorded

    params = {
        "target_pane_id": anchor,
        "direction": parent.get("direction") or "right",
        "focus": bool(config.get("focus", True)),
    }  # type: Dict[str, Any]
    if parent.get("ratio") is not None:
        params["ratio"] = parent["ratio"]
    if record.get("cwd"):
        params["cwd"] = record["cwd"]
    if record.get("env"):
        params["env"] = record["env"]

    result = api.call("pane.split", params)
    new_pane_id = None
    if isinstance(result, dict):
        new_pane_id = result.get("pane_id")
        pane = result.get("pane")
        if not new_pane_id and isinstance(pane, dict):
            new_pane_id = pane.get("pane_id")
    if not new_pane_id:
        raise ValueError("pane.split did not return a pane_id")

    label = record.get("label")
    if label:
        # Best effort: the pane is already back, and losing its label is not
        # worth throwing away a successful restore.
        try:
            api.call("pane.rename", {"pane_id": new_pane_id, "label": label})
        except Exception as exc:
            sys.stderr.write("herdr-undo-close: could not rename restored pane (%s)\n" % exc)

    if parent.get("dead_side") == "first":
        try:
            api.call(
                "pane.swap", {"source_pane_id": new_pane_id, "target_pane_id": anchor}
            )
        except Exception as exc:
            # The pane lives, it is just on the wrong side of the split.
            sys.stderr.write("herdr-undo-close: could not swap restored pane (%s)\n" % exc)
            exact = False

    plan = relaunch_mod.relaunch_plan(record.get("relaunch"), config)
    relaunched = run_pane_relaunch(api, new_pane_id, plan)

    agent = record.get("agent")
    if relaunched and plan and plan.get("kind") == "agent":
        # The agent is running again, so the "was not restarted" caveat is
        # no longer true and must not be printed.
        agent = None

    return {
        "kind": "pane",
        "new_pane_id": new_pane_id,
        "tab_id": tab_id,
        "tab_label": record.get("tab_label", ""),
        "label": label or "",
        "cwd": record.get("cwd") or "",
        "exact": exact,
        "agent": agent,
        # pane.split still cannot take a launch command, so a pane that had
        # one lost it. Only worth saying when nothing else was typed into
        # the pane: if the process is back, the user has what they wanted
        # and the missing `command` is a distinction without a difference.
        "command_lost": bool(record.get("command")) and not relaunched,
        "relaunched": [plan] if relaunched else [],
        "not_relaunched": [plan] if plan and not plan.get("allowed") else [],
        "relaunch_failed": [plan] if (plan and plan.get("allowed") and not relaunched) else [],
    }


def _same_entry(candidate, record, kind):
    # type: (Dict[str, Any], Dict[str, Any], str) -> bool
    """Identity test used to remove a restored entry from `closed`.

    Kind is part of the identity: a closed pane and its own tab's closed entry
    can share a tab_id, and a pass that saw both stamps them with the same
    closed_ms, so tab_id plus closed_ms alone is not unique.
    """
    if store.entry_kind(candidate) != kind:
        return False
    if candidate.get("closed_ms") != record.get("closed_ms"):
        return False
    if kind == "pane":
        return candidate.get("pane_id") == record.get("pane_id")
    return candidate.get("tab_id") == record.get("tab_id")


def reopen_at(api, state_dir, config, env_workspace_id, index=0):
    # type: (HerdrApi, str, Dict[str, Any], Optional[str], int) -> Optional[Dict[str, Any]]
    """Restore the closed entry at `index` (0 is whatever died most recently,
    tab or pane). Returns None when there is nothing at that index. The entry
    is only removed from `closed` once the restore has actually succeeded."""
    with store.locked(state_dir):
        state = store.load_state(state_dir)
        closed = state.get("closed", [])
        if index >= len(closed):
            return None
        record = copy.deepcopy(closed[index])

    kind = store.entry_kind(record)
    if kind == "pane":
        result = restore_pane_record(api, record, config)
    else:
        workspaces = api.call("workspace.list", {}).get("workspaces", [])
        result = restore_record(
            api, record, workspaces, env_workspace_id, config["focus"], config
        )

    with store.locked(state_dir):
        state = store.load_state(state_dir)
        state["closed"] = [
            c for c in state.get("closed", []) if not _same_entry(c, record, kind)
        ]
        store.save_state(state_dir, state)

    return result


def reopen_last(api, state_dir, config, env_workspace_id):
    # type: (HerdrApi, str, Dict[str, Any], Optional[str]) -> Optional[Dict[str, Any]]
    """Restore whatever died most recently, tab or pane (action reopen-last)."""
    return reopen_at(api, state_dir, config, env_workspace_id, index=0)
