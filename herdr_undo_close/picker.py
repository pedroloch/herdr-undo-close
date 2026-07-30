"""Zero-dependency popup TUI for picking a closed tab or pane to reopen.

Runs as the `picker` popup pane's command (the `pick` subcommand). A popup
pane is session-modal and closes when its command exits, so this simply
runs a small raw-mode loop on /dev/tty-equivalent stdin and exits once a
choice (or a cancel) is made.

The row helpers here are also what the plain `list` subcommand prints, so a
row reads the same in both places. One stack holds both kinds of entry, so
every row leads with its kind: a tab row is named by its tab label, a pane
row by the pane's own label (or its cwd's basename, since panes usually have
no label) plus the tab it belongs to.
"""

import os
import select
import sys
import time
from typing import Any, Dict, List, Optional

from . import store
from .api import HerdrApi
from .restore import PaneTabGone, reopen_at

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - plugin only targets linux/macos
    termios = None
    tty = None


def format_age(now_ms, then_ms):
    # type: (int, int) -> str
    delta_s = max(0, (now_ms - then_ms) // 1000)
    if delta_s < 60:
        return "%ds ago" % delta_s
    delta_m = delta_s // 60
    if delta_m < 60:
        return "%dm ago" % delta_m
    delta_h = delta_m // 60
    if delta_h < 24:
        return "%dh ago" % delta_h
    delta_d = delta_h // 24
    return "%dd ago" % delta_d


def _row_key(index):
    # type: (int) -> str
    return "0" if index == 9 else str(index + 1)


def pane_name(label, cwd, fallback="pane"):
    # type: (Optional[str], Optional[str], str) -> str
    """A human name for a pane: its label, else its cwd's basename, else the
    fallback. Panes are usually unlabelled, so the basename is what a user
    actually recognises."""
    if label:
        return label
    base = os.path.basename((cwd or "").rstrip("/"))
    return base or fallback


def row_kind(record):
    # type: (Dict[str, Any]) -> str
    return store.entry_kind(record)


def row_title(record):
    # type: (Dict[str, Any]) -> str
    if store.entry_kind(record) == "pane":
        name = pane_name(record.get("label"), record.get("cwd"), record.get("pane_id") or "pane")
        return "%s in %s" % (name, record.get("tab_label") or "?")
    return record.get("label") or ""


def row_detail(record):
    # type: (Dict[str, Any]) -> str
    """The kind-specific column: how big a tab was, or how a pane was split.

    The split ratio is deliberately left out - it would push the row past 80
    columns, and `list --json` has it for anyone who needs the exact number.
    """
    if store.entry_kind(record) == "pane":
        parent = record.get("parent")
        if not isinstance(parent, dict):
            parent = {}
        return "split:%-6s agent:%s" % (
            (parent.get("direction") or "?")[:6],
            record.get("agent") or "-",
        )

    panes = record.get("panes", [])
    agent_kinds = sorted({p.get("agent") for p in panes if p.get("agent")})
    return "panes:%-2d agents:%s" % (len(panes), ",".join(agent_kinds) if agent_kinds else "-")


def row_label(index, record, now_ms):
    # type: (int, Dict[str, Any], int) -> str
    return "%s. %-4s %-22s %-10s %-26s %s" % (
        _row_key(index),
        row_kind(record),
        row_title(record)[:22],
        (record.get("workspace_label") or "")[:10],
        row_detail(record)[:26],
        format_age(now_ms, record.get("closed_ms", now_ms)),
    )


def render_lines(closed, selected, now_ms):
    # type: (List[Dict[str, Any]], int, int) -> List[str]
    lines = [
        "Reopen closed tab or pane  (up/down or 1-9,0 select, enter restores, q cancels)",
        "",
    ]
    for i, record in enumerate(closed[:10]):
        marker = ">" if i == selected else " "
        lines.append("%s %s" % (marker, row_label(i, record, now_ms)))
    return lines


def _read_key(fd):
    # type: (int) -> str
    """Read one logical keypress: a printable char, or 'up'/'down'/'esc'/'eof'
    for escape sequences and end-of-input. A bare Escape (no following bytes
    within ~10ms) is reported as 'esc' rather than blocking forever waiting
    for more bytes. EOF (os.read returning b"") is reported as 'eof' - it is
    not a printable key and must not be mistaken for one (an empty string is,
    confusingly, "in" every non-empty string, including the digit set)."""
    chunk = os.read(fd, 1)
    if not chunk:
        return "eof"
    ch = chunk.decode("utf-8", "ignore")
    if ch != "\x1b":
        return ch
    ready, _, _ = select.select([fd], [], [], 0.01)
    if not ready:
        return "esc"
    rest = os.read(fd, 2).decode("utf-8", "ignore")
    if rest == "[A":
        return "up"
    if rest == "[B":
        return "down"
    return "esc"


def _wait_any_key(fd):
    # type: (int) -> None
    os.read(fd, 1)


def run_picker(state_dir, api, config, env_workspace_id):
    # type: (str, HerdrApi, Dict[str, Any], Optional[str]) -> int
    state = store.load_state(state_dir)
    closed = state.get("closed", [])
    now_ms = int(time.time() * 1000)

    if not sys.stdin.isatty() or termios is None:
        if not closed:
            print("Nothing to reopen")
        else:
            for line in render_lines(closed, 0, now_ms):
                print(line)
        return 0

    if not closed:
        print("Nothing to reopen")
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            _wait_any_key(fd)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return 0

    visible = min(len(closed), 10)
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    selected = 0
    result_index = None  # type: Optional[int]
    try:
        tty.setraw(fd)
        while True:
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.write("\r\n".join(render_lines(closed, selected, now_ms)) + "\r\n")
            sys.stdout.flush()

            key = _read_key(fd)
            if key in ("q", "esc", "\x03", "eof"):
                break
            if key in ("k", "up"):
                selected = (selected - 1) % visible
            elif key in ("j", "down"):
                selected = (selected + 1) % visible
            elif key in ("\r", "\n"):
                result_index = selected
                break
            elif key == "0":
                if len(closed) >= 10:
                    result_index = 9
                    break
            elif len(key) == 1 and key.isdigit() and key != "0":
                idx = int(key) - 1
                if idx < len(closed):
                    result_index = idx
                    break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    if result_index is None:
        return 0

    try:
        result = reopen_at(api, state_dir, config, env_workspace_id, index=result_index)
    except PaneTabGone:
        # The popup is about to close, so this is the only chance to say why.
        print("Cannot reopen that pane: its tab is gone")
        return 1

    if result is None:
        return 0
    if result.get("kind") == "pane":
        print("Reopened pane %s" % pane_name(result.get("label"), result.get("cwd")))
    else:
        print("Reopened %s" % result.get("label", ""))
    return 0
