"""Work out what a pane was RUNNING, and how to bring it back.

A restore that hands back a bare shell misses the point of most tabs: they
hold an editor or a coding agent, not an idle prompt. Reopening a tab whose
pane ran neovim and getting a shell in the right directory is a layout
restore, not an undo.

Everything about "bring the process back" lives here, because two thirds of
it reach outside Herdr's own model and deserve to be easy to find and easy
to distrust:

* Reading `pane.process_info` and turning it into a relaunch intent.
* Resolving a claude session id from `~/.claude/projects`, which is another
  tool's private on-disk layout, and therefore a heuristic - see
  find_claude_session_id.
* Deciding what may be re-run at all. Re-running an arbitrary command is a
  footgun: reopening a tab that held a half-finished migration or a deploy
  must not replay it. Only an allowlist of interactive, idempotent programs
  is ever relaunched, plus agents, which are their own category because
  resuming a conversation is not the same as re-running a command.

The two restore paths need the answer in two different shapes, and both get
it from the same plan dict:

* a TAB restore puts `command` on the pane node it hands to layout.apply,
  which honours it (measured against Herdr 0.7.5: a node carrying
  `command: ["nvim","README.md"]` came back with nvim in the foreground);
* a PANE restore has no such door - `pane.split` takes `cwd` and `env` but
  no command - so it types the line into the new pane's shell instead,
  after POLLING for readiness. Never after a fixed sleep: a slow shell
  profile swallows keystrokes typed before the prompt exists, and the
  keystrokes are gone with no error to notice.
"""

import os
import shlex
import time
from typing import Any, Dict, List, Optional

# Readiness polling for a freshly split pane. Module constants rather than
# config keys: this is a property of how fast a shell starts, not a taste.
READY_TIMEOUT_S = 10.0
READY_INTERVAL_S = 0.2

# Command-line flag each known agent uses to resume one exact session.
# Anything not listed is deliberately not guessed at. There is no
# "continue the most recent one" fallback: with no session id, the agent
# is launched plainly instead (see agent_command).
AGENT_RESUME_FLAGS = {
    "claude": "-r",
    "opencode": "-s",
}  # type: Dict[str, str]


def claude_projects_dir():
    # type: () -> str
    """Where claude keeps its conversation logs. A separate function so it
    can be pointed elsewhere in tests - the suite must never read the real
    ~/.claude."""
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def project_slug(cwd):
    # type: (str) -> str
    """claude's directory name for a working directory: the absolute path
    with every "/" turned into "-". Measured: /private/tmp/x/resume-test
    became -private-tmp-x-resume-test."""
    return cwd.replace("/", "-")


def find_claude_session_id(cwd):
    # type: (Optional[str]) -> Optional[str]
    """Newest claude session id for a working directory, or None.

    HEURISTIC, and knowingly so: it reads another tool's private layout,
    `~/.claude/projects/<slug>/<session-id>.jsonl`, which claude is free to
    change. Herdr reports `agent_session` for opencode but not for claude
    (measured on four live agents), and resuming the right conversation is
    the whole point of restoring an agent pane, so the lookup is worth it -
    but every unexpected shape falls back to None, which simply means a
    plain `claude` launch instead of `claude -r <id>`, not an error.

    Known limitation: two claude panes sharing one working directory cannot
    be told apart here, so both resolve to the same newest session.
    """
    if not cwd or not isinstance(cwd, str):
        return None
    directory = os.path.join(claude_projects_dir(), project_slug(cwd))
    try:
        names = [n for n in os.listdir(directory) if n.endswith(".jsonl")]
    except OSError:
        return None

    newest = None  # type: Optional[str]
    newest_mtime = None  # type: Optional[float]
    for name in names:
        try:
            mtime = os.path.getmtime(os.path.join(directory, name))
        except OSError:
            continue
        if newest_mtime is None or mtime > newest_mtime:
            newest, newest_mtime = name, mtime

    if newest is None:
        return None
    return newest[: -len(".jsonl")]


def reported_session_id(pane_entry):
    # type: (Dict[str, Any]) -> Optional[str]
    """The session id Herdr itself reported for this pane, if any.

    agent.list carries `agent_session` for opencode and not for claude, so
    this is the good path when it exists and silence otherwise. Only
    kind == "id" is used: a "path" ref is a filesystem path, not something
    that can be handed to `-r` or `-s`.
    """
    session = pane_entry.get("agent_session")
    if not isinstance(session, dict):
        return None
    if session.get("kind") not in (None, "id"):
        return None
    value = session.get("value")
    return value if isinstance(value, str) and value else None


def build_agent_intent(pane_entry):
    # type: (Dict[str, Any]) -> Optional[Dict[str, Any]]
    """Relaunch intent for a pane that was running an agent, or None when it
    was not. A session id that cannot be found is not an error: the agent
    still comes back, launched plainly with no conversation to resume."""
    agent = pane_entry.get("agent")
    if not agent:
        return None
    session_id = reported_session_id(pane_entry)
    if session_id is None and agent == "claude":
        session_id = find_claude_session_id(pane_entry.get("cwd"))
    return {"kind": "agent", "agent": agent, "session_id": session_id}


def build_process_intent(info):
    # type: (Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]
    """Relaunch intent from one pane.process_info result, or None for an
    idle shell.

    A pane sitting at its prompt reports the shell itself as the foreground
    process, so the only thing worth recording is a foreground process whose
    pid differs from `shell_pid`. When `shell_pid` is missing that
    comparison cannot be made at all, and guessing would record "-zsh" as a
    program the user was running.
    """
    if not isinstance(info, dict):
        return None
    shell_pid = info.get("shell_pid")
    if shell_pid is None:
        return None
    for proc in info.get("foreground_processes") or []:
        if not isinstance(proc, dict):
            continue
        pid = proc.get("pid")
        if pid is None or pid == shell_pid:
            continue
        argv = [a for a in (proc.get("argv") or []) if isinstance(a, str)]
        if not argv:
            continue
        return {"kind": "process", "argv": argv}
    return None


def unwrap_process_info(result):
    # type: (Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]
    """Pull the inner payload out of a pane.process_info result.

    The real socket answers with an envelope - {"type": "pane_process_info",
    "process_info": {...}} - never the payload itself (measured live: reading
    "shell_pid" straight off the envelope is always None, since it is one
    level too shallow). A missing or malformed "process_info" is "no
    information", matching what build_process_intent and shell_is_ready
    already do with None - so this never raises, it just does dict lookups.
    """
    info = result.get("process_info") if isinstance(result, dict) else None
    return info if isinstance(info, dict) else None


def build_pane_intent(api, pane_entry):
    # type: (Any, Dict[str, Any]) -> Optional[Dict[str, Any]]
    """How this pane could be brought back, or None when nothing should be.

    One pane.process_info call at most, and only for panes with no agent -
    an agent pane is already fully described by agent.list. Any failure
    means "no intent this pass", never a failed pass: the pane's layout
    record is far more valuable than its process, and the next pass tries
    again.
    """
    intent = build_agent_intent(pane_entry)
    if intent is not None:
        return intent

    pane_id = pane_entry.get("pane_id")
    if not pane_id:
        return None
    try:
        result = api.call("pane.process_info", {"pane_id": pane_id})
        return build_process_intent(unwrap_process_info(result))
    except Exception:
        return None


def attach_intents(api, panes):
    # type: (Any, List[Dict[str, Any]]) -> None
    """Attach a "relaunch" key to every pane that has something to bring
    back. Panes sitting at an idle prompt get no key at all, which is what
    makes a restored plain shell stay a plain shell. Mutates panes."""
    for entry in panes:
        intent = build_pane_intent(api, entry)
        if intent is not None:
            entry["relaunch"] = intent


def agent_command(agent, session_id):
    # type: (Optional[str], Optional[str]) -> Optional[List[str]]
    """Command line that brings an agent back, or None for an agent kind we
    do not know how to resume. Guessing at an unknown agent's flags would
    mean running an unknown program with unknown arguments.

    With no session id there is nothing to resume, and no continue-flag
    fallback either: `claude -c` (or any equivalent) fails outright in a
    directory with no prior conversation, killing the pane instead of
    holding an agent. So a missing session id means a plain launch -
    [agent] - not a guess at "the most recent conversation"."""
    resume_flag = AGENT_RESUME_FLAGS.get(agent or "")
    if not resume_flag:
        return None
    if session_id:
        return [agent, resume_flag, session_id]
    return [agent]


def relaunch_plan(intent, config):
    # type: (Optional[Dict[str, Any]], Dict[str, Any]) -> Optional[Dict[str, Any]]
    """Turn a recorded intent into a decision, or None when there is nothing
    to decide.

        {"kind": "agent"|"process",
         "program": "claude"|"nvim",     # what to call it in a message
         "display": "nvim README.md",    # the full command line, for humans
         "command": [...] | None,        # what to actually run, if allowed
         "allowed": bool,
         "session_id": "abc"|None}       # agent only: was a session resumed?

    None and allowed == False are different answers on purpose. None means
    "say nothing": there was no intent, or the user switched the feature
    off, so the old caveats stand unchanged. allowed == False means "we know
    what it was and deliberately did not re-run it", which the user is told
    about, because a tab that comes back without its psql is otherwise just
    a tab that lost something.
    """
    if not isinstance(intent, dict):
        return None
    if not config.get("restore_process", True):
        return None

    kind = intent.get("kind")
    if kind == "agent":
        if not config.get("relaunch_agents", True):
            return None
        agent = intent.get("agent") or ""
        session_id = intent.get("session_id")
        command = agent_command(agent, session_id)
        return {
            "kind": "agent",
            "program": agent,
            "display": agent,
            "command": command,
            "allowed": command is not None,
            "session_id": session_id,
        }

    if kind == "process":
        argv = [a for a in (intent.get("argv") or []) if isinstance(a, str)]
        if not argv:
            return None
        # By basename, so /usr/bin/nvim is as allowed as nvim.
        program = os.path.basename(argv[0])
        allowed = program in set(config.get("relaunch_programs") or [])
        return {
            "kind": "process",
            "program": program,
            "display": " ".join(argv),
            "command": list(argv) if allowed else None,
            "allowed": allowed,
        }

    return None


def describe_relaunched(plan):
    # type: (Dict[str, Any]) -> str
    if plan.get("kind") == "agent":
        program = plan.get("program", "agent")
        if plan.get("session_id"):
            return "%s conversation resumed" % program
        return "%s restarted, no conversation to resume" % program
    return "%s relaunched" % plan.get("display", plan.get("program", "process"))


def describe_skipped(plan):
    # type: (Dict[str, Any]) -> str
    return "was running %s, not relaunched" % plan.get("program", "something")


def describe_failed(plan):
    # type: (Dict[str, Any]) -> str
    return "could not relaunch %s" % plan.get("program", "it")


def shell_is_ready(info):
    # type: (Optional[Dict[str, Any]]) -> bool
    """True when a pane.process_info result describes a shell sitting at its
    prompt: a known shell_pid and no foreground process other than the shell
    itself."""
    if not isinstance(info, dict):
        return False
    shell_pid = info.get("shell_pid")
    if not shell_pid:
        return False
    for proc in info.get("foreground_processes") or []:
        if not isinstance(proc, dict):
            continue
        pid = proc.get("pid")
        if pid is not None and pid != shell_pid:
            return False
    return True


def wait_for_shell(api, pane_id, timeout_s=None, interval_s=None, sleep=None, clock=None):
    # type: (Any, str, Optional[float], Optional[float], Optional[Any], Optional[Any]) -> bool
    """Poll until a freshly created pane's shell is ready to be typed into.

    A fixed sleep is not an option: a shell with a slow profile is still
    starting when the sleep ends, and text sent before the prompt exists is
    swallowed with no error anywhere. So this asks pane.process_info until
    the shell is idle, and returns False on timeout rather than typing into
    the void. A failing query counts as "not ready yet", not as a reason to
    stop: a pane that was created milliseconds ago may not be answerable
    yet.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    timeout_s = READY_TIMEOUT_S if timeout_s is None else timeout_s
    interval_s = READY_INTERVAL_S if interval_s is None else interval_s

    deadline = clock() + timeout_s
    while True:
        try:
            result = api.call("pane.process_info", {"pane_id": pane_id})
            info = unwrap_process_info(result)
        except Exception:
            info = None
        if shell_is_ready(info):
            return True
        if clock() >= deadline:
            return False
        sleep(interval_s)


def command_line(argv):
    # type: (List[str]) -> str
    """One shell line from an argv, quoted element by element so a file name
    with a space survives the round trip through the shell we are typing
    into."""
    return " ".join(shlex.quote(a) for a in argv)


def send_command(api, pane_id, command):
    # type: (Any, str, List[str]) -> None
    """Type a command line into a pane and press enter."""
    api.call("pane.send_text", {"pane_id": pane_id, "text": command_line(command) + "\n"})
