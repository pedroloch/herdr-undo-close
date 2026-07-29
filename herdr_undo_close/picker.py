"""Zero-dependency popup TUI for picking a closed tab to reopen.

Runs as the `picker` popup pane's command (the `pick` subcommand). A popup
pane is session-modal and closes when its command exits, so this simply
runs a small raw-mode loop on /dev/tty-equivalent stdin and exits once a
choice (or a cancel) is made.
"""

import os
import select
import sys
import time
from typing import Any, Dict, List, Optional

from . import store
from .api import HerdrApi
from .restore import reopen_at

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


def row_label(index, record, now_ms):
    # type: (int, Dict[str, Any], int) -> str
    panes = record.get("panes", [])
    agent_kinds = sorted({p.get("agent") for p in panes if p.get("agent")})
    agents_txt = ",".join(agent_kinds) if agent_kinds else "-"
    return "%s. %-20s %-12s panes:%-2d agents:%-10s %s" % (
        _row_key(index),
        (record.get("label") or "")[:20],
        (record.get("workspace_label") or "")[:12],
        len(panes),
        agents_txt,
        format_age(now_ms, record.get("closed_ms", now_ms)),
    )


def render_lines(closed, selected, now_ms):
    # type: (List[Dict[str, Any]], int, int) -> List[str]
    lines = [
        "Reopen closed tab  (up/down or 1-9,0 select, enter restores, q cancels)",
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

    result = reopen_at(api, state_dir, config, env_workspace_id, index=result_index)
    if result is not None:
        print("Reopened %s" % result.get("label", ""))
    return 0
