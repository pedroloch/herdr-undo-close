"""State file: load, atomic save, locking, and trimming.

State lives at $HERDR_PLUGIN_STATE_DIR/state.json:

    {"version": 2, "index": {"<tab_id>": <record>}, "closed": [<record>, ...]}

Version 2 added closed-pane entries, so `closed` is no longer homogeneous:
entries are discriminated by "kind" ("tab" or "pane"). A version 1 file has
no "kind" at all and every entry in it is a tab, which is why readers must
treat a missing "kind" as "tab" instead of rejecting the entry - real users
have a populated stack and upgrading the plugin must not throw it away.
`closed` is one stack across both kinds, ordered newest death first, so
max_entries is a single shared budget and not one budget per kind.

Several event hooks can run concurrently, so every read-modify-write cycle
must happen while holding an exclusive fcntl.flock on a sibling state.lock.
Writes go through a temp file plus os.replace so a crash mid-write cannot
leave a half-written state.json behind. A corrupt or unreadable state file
must never wedge the plugin: log to stderr and start from an empty state.
"""

import contextlib
import fcntl
import json
import os
import sys
import tempfile
from typing import Any, Dict, Iterator

STATE_FILENAME = "state.json"
LOCK_FILENAME = "state.lock"
STATE_VERSION = 2


def default_state():
    # type: () -> Dict[str, Any]
    return {"version": STATE_VERSION, "index": {}, "closed": []}


def entry_kind(entry):
    # type: (Dict[str, Any]) -> str
    """Kind of a `closed` entry: "pane" or "tab".

    A version 1 state file has no "kind" key, and every entry it holds is a
    closed tab, so anything that is not explicitly "pane" reads as "tab".
    This is the single place that decision is made; every caller that
    dispatches on kind goes through here.
    """
    return "pane" if entry.get("kind") == "pane" else "tab"


def _state_path(state_dir):
    # type: (str) -> str
    return os.path.join(state_dir, STATE_FILENAME)


def _lock_path(state_dir):
    # type: (str) -> str
    return os.path.join(state_dir, LOCK_FILENAME)


def load_state(state_dir):
    # type: (str) -> Dict[str, Any]
    """Load state.json, tolerating a missing, corrupt, or malformed file.

    Callers doing a read-modify-write cycle must call this while holding
    `locked(state_dir)`.
    """
    path = _state_path(state_dir)
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return default_state()
    except (OSError, ValueError) as exc:
        sys.stderr.write("herdr-undo-close: state.json unreadable (%s), starting fresh\n" % exc)
        return default_state()

    if (
        not isinstance(data, dict)
        or not isinstance(data.get("index"), dict)
        or not isinstance(data.get("closed"), list)
    ):
        sys.stderr.write("herdr-undo-close: state.json has an unexpected shape, starting fresh\n")
        return default_state()

    data.setdefault("version", STATE_VERSION)
    return data


def save_state(state_dir, state):
    # type: (str, Dict[str, Any]) -> None
    """Atomically write state.json via a temp file plus os.replace."""
    os.makedirs(state_dir, exist_ok=True)
    path = _state_path(state_dir)
    fd, tmp_path = tempfile.mkstemp(prefix=".state.", suffix=".json.tmp", dir=state_dir)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def locked(state_dir):
    # type: (str) -> Iterator[None]
    """Hold an exclusive flock on state.lock for the duration of the block."""
    os.makedirs(state_dir, exist_ok=True)
    lock_file = open(_lock_path(state_dir), "w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def trim(state, max_entries, max_index=500):
    # type: (Dict[str, Any], int, int) -> Dict[str, Any]
    """Cap `closed` to max_entries and `index` to the max_index most recently
    refreshed entries. Mutates and returns state.

    max_entries applies across both kinds of `closed` entry: the stack is one
    death-ordered history, so a burst of closed panes is allowed to push old
    closed tabs out exactly as a burst of closed tabs would.
    """
    closed = state.get("closed", [])
    if len(closed) > max_entries:
        state["closed"] = closed[:max_entries]

    index = state.get("index", {})
    if len(index) > max_index:
        ordered = sorted(
            index.items(),
            key=lambda kv: kv[1].get("refreshed_ms", 0),
            reverse=True,
        )[:max_index]
        state["index"] = dict(ordered)

    return state
