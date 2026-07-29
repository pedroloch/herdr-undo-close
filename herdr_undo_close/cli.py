"""Subcommand dispatch: python3 -m herdr_undo_close.cli <subcommand>.

Herdr runs the manifest's commands with the plugin directory as the cwd, so
`-m` resolves this package. Subcommands:

    record        event hook + startup hook: reconcile and refresh
    reopen        action reopen-last: restore the most recently closed tab
    open-picker   action pick: open the picker popup pane
    pick          the picker popup pane's own command
    list          plain CLI: print (or --json) the closed stack
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import config as config_mod
from . import record as record_mod
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


def _summary_body(result):
    # type: (Dict[str, Any]) -> str
    parts = ["%d pane(s)" % result["pane_count"]]
    if result["agent_kinds"]:
        parts.append("%s was not restarted" % ", ".join(result["agent_kinds"]))
    if result["fallback_used"]:
        parts.append("original workspace was gone, reopened here instead")
    if not result["position_restored"]:
        parts.append("position could not be restored")
    return "; ".join(parts)


def _summary_line(result):
    # type: (Dict[str, Any]) -> str
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
            record_mod.run_record_pass(api, state_dir, cfg["max_entries"], forced_tab_id=forced_tab_id)
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
                _notify(api, "Reopened %s" % result["label"], _summary_body(result))
            print(_summary_line(result))
    except HerdrApiError as exc:
        sys.stderr.write("herdr-undo-close: reopen failed: %s\n" % exc)
        return 1
    return 0


def cmd_pick(argv):
    # type: (List[str]) -> int
    state_dir = _state_dir()
    cfg = config_mod.load_config()
    env_workspace_id = os.environ.get("HERDR_WORKSPACE_ID")

    from . import picker

    try:
        api = HerdrApi()
        return picker.run_picker(state_dir, api, cfg, env_workspace_id)
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
        panes = record.get("panes", [])
        agent_kinds = sorted({p.get("agent") for p in panes if p.get("agent")})
        print(
            "%2d. %-20s workspace=%-12s panes=%-2d agents=%s"
            % (
                i + 1,
                record.get("label", ""),
                record.get("workspace_label", ""),
                len(panes),
                ",".join(agent_kinds) if agent_kinds else "-",
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
