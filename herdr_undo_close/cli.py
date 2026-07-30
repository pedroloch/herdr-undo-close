"""Subcommand dispatch: python3 -m herdr_undo_close.cli <subcommand>.

Herdr runs the manifest's commands with the plugin directory as the cwd, so
`-m` resolves this package. Subcommands:

    record        event hook + startup hook: reconcile and refresh
    reopen        action reopen-last: restore whatever died last, tab or pane
    open-picker   action pick: open the picker popup pane
    pick          the picker popup pane's own command
    list          plain CLI: print (or --json) the closed stack
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import config as config_mod
from . import picker as picker_mod
from . import record as record_mod
from . import relaunch as relaunch_mod
from . import restore as restore_mod
from . import store
from .api import HerdrApi, HerdrApiError


def _env_json(name):
    # type: (str) -> Dict[str, Any]
    raw = os.environ.get(name)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _state_dir():
    # type: () -> str
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if not state_dir:
        sys.stderr.write("herdr-undo-close: HERDR_PLUGIN_STATE_DIR is not set\n")
        sys.exit(1)
    return state_dir


def _notify(api, title, body=""):
    # type: (HerdrApi, str, str) -> None
    try:
        api.call("notification.show", {"title": title, "body": body})
    except HerdrApiError:
        pass


def _pane_title(result):
    # type: (Dict[str, Any]) -> str
    """What to call a restored pane: its label, else its cwd's basename. Same
    naming the picker and `list` use, so a restore reads like its row did."""
    return picker_mod.pane_name(result.get("label"), result.get("cwd"))


def _relaunch_parts(result):
    # type: (Dict[str, Any]) -> List[str]
    """What happened to the things that were running, in the three outcomes
    a user has to be able to tell apart: brought back, deliberately left
    alone, or tried and failed. Identical phrases are collapsed - a tab with
    four nvim panes says it once - but nothing is summarised away."""
    parts = []  # type: List[str]
    for key, describe in (
        ("relaunched", relaunch_mod.describe_relaunched),
        ("not_relaunched", relaunch_mod.describe_skipped),
        ("relaunch_failed", relaunch_mod.describe_failed),
    ):
        for plan in result.get(key) or []:
            phrase = describe(plan)
            if phrase not in parts:
                parts.append(phrase)
    return parts


def _summary_body(result):
    # type: (Dict[str, Any]) -> str
    """The caveats, spelled out. Everything a restore could not bring back is
    listed here, because the alternative is a user who thinks it did."""
    if result.get("kind") == "pane":
        parts = ["in tab '%s'" % result.get("tab_label", "")]
        parts.extend(_relaunch_parts(result))
        if not result.get("exact"):
            parts.append("split position is approximate")
        if result.get("agent"):
            parts.append("%s was not restarted" % result["agent"])
        if result.get("command_lost"):
            parts.append("launch command was not replayed")
        return "; ".join(parts)

    parts = ["%d pane(s)" % result["pane_count"]]
    parts.extend(_relaunch_parts(result))
    if result["agent_kinds"]:
        parts.append("%s was not restarted" % ", ".join(result["agent_kinds"]))
    if result["fallback_used"]:
        parts.append("original workspace was gone, reopened here instead")
    if not result["position_restored"]:
        parts.append("position could not be restored")
    return "; ".join(parts)


def _summary_title(result):
    # type: (Dict[str, Any]) -> str
    if result.get("kind") == "pane":
        return "Reopened pane %s" % _pane_title(result)
    return "Reopened %s" % result.get("label", "")


def _summary_line(result):
    # type: (Dict[str, Any]) -> str
    if result.get("kind") == "pane":
        return "reopened pane '%s' as %s (%s)" % (
            _pane_title(result),
            result["new_pane_id"],
            _summary_body(result),
        )
    return "reopened '%s' as %s (%s)" % (
        result["label"],
        result["new_tab_id"],
        _summary_body(result),
    )


def cmd_record(argv):
    # type: (List[str]) -> int
    state_dir = _state_dir()
    cfg = config_mod.load_config()

    forced_tab_id = None
    if os.environ.get("HERDR_PLUGIN_EVENT") == "tab.closed":
        data = _env_json("HERDR_PLUGIN_EVENT_JSON").get("data", {})
        if isinstance(data, dict):
            forced_tab_id = data.get("tab_id")

    try:
        with HerdrApi() as api:
            record_mod.run_record_pass(
                api,
                state_dir,
                cfg["max_entries"],
                forced_tab_id=forced_tab_id,
                undo_panes=cfg["undo_panes"],
                restore_process=cfg["restore_process"],
            )
    except HerdrApiError as exc:
        sys.stderr.write("herdr-undo-close: record failed: %s\n" % exc)
        return 1
    return 0


def cmd_reopen(argv):
    # type: (List[str]) -> int
    state_dir = _state_dir()
    cfg = config_mod.load_config()
    env_workspace_id = os.environ.get("HERDR_WORKSPACE_ID")

    try:
        with HerdrApi() as api:
            result = restore_mod.reopen_last(api, state_dir, cfg, env_workspace_id)
            if result is None:
                if cfg["notify"]:
                    _notify(api, "Nothing to reopen")
                print("Nothing to reopen")
                return 0
            if cfg["notify"]:
                _notify(api, _summary_title(result), _summary_body(result))
            print(_summary_line(result))
    except restore_mod.PaneTabGone as exc:
        # Not a bug, so it must not read like one: the pane's tab closed after
        # the pane did, and there is nothing left to split. The next record
        # pass prunes the entry. `api` is still usable here - HerdrApi holds no
        # connection between calls.
        if cfg["notify"]:
            _notify(api, "Cannot reopen pane", "its tab is gone")
        sys.stderr.write("herdr-undo-close: cannot reopen pane: its tab is gone (%s)\n" % exc)
        print("Cannot reopen that pane: its tab is gone")
        return 1
    except HerdrApiError as exc:
        sys.stderr.write("herdr-undo-close: reopen failed: %s\n" % exc)
        return 1
    return 0


def cmd_pick(argv):
    # type: (List[str]) -> int
    state_dir = _state_dir()
    cfg = config_mod.load_config()
    env_workspace_id = os.environ.get("HERDR_WORKSPACE_ID")

    try:
        api = HerdrApi()
        return picker_mod.run_picker(state_dir, api, cfg, env_workspace_id)
    except HerdrApiError as exc:
        sys.stderr.write("herdr-undo-close: pick failed: %s\n" % exc)
        return 1


def cmd_open_picker(argv):
    # type: (List[str]) -> int
    plugin_id = os.environ.get("HERDR_PLUGIN_ID", "")
    try:
        with HerdrApi() as api:
            api.call("plugin.pane.open", {"plugin_id": plugin_id, "entrypoint": "picker"})
    except HerdrApiError as exc:
        sys.stderr.write("herdr-undo-close: open-picker failed: %s\n" % exc)
        return 1
    return 0


def cmd_list(argv):
    # type: (List[str]) -> int
    state_dir = _state_dir()
    state = store.load_state(state_dir)
    closed = state.get("closed", [])
    as_json = "--json" in argv

    if as_json:
        print(json.dumps(closed, indent=2, sort_keys=True))
        return 0

    if not closed:
        print("Nothing to reopen")
        return 0

    for i, record in enumerate(closed):
        print(
            "%2d. %-4s %-24s workspace=%-12s %s"
            % (
                i + 1,
                picker_mod.row_kind(record),
                picker_mod.row_title(record)[:24],
                (record.get("workspace_label", "") or "")[:12],
                picker_mod.row_detail(record),
            )
        )
    return 0


COMMANDS = {
    "record": cmd_record,
    "reopen": cmd_reopen,
    "pick": cmd_pick,
    "open-picker": cmd_open_picker,
    "list": cmd_list,
}


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("usage: python3 -m herdr_undo_close.cli <%s>\n" % "|".join(sorted(COMMANDS)))
        return 2

    sub, rest = argv[0], argv[1:]
    handler = COMMANDS.get(sub)
    if handler is None:
        sys.stderr.write("herdr-undo-close: unknown subcommand %r\n" % sub)
        return 2
    return handler(rest)


if __name__ == "__main__":
    sys.exit(main())
