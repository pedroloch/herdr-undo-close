# herdr-undo-close

Reopen a closed tab, or a closed pane, in Herdr, the way Cmd+Shift+T reopens a
closed tab in a browser.

## What it does, and why

Herdr does not have an undo-close. Once a tab is gone, its split layout,
per-pane working directories, and launch commands are gone with it: the
`tab.closed` event Herdr fires carries no tab content at all, just an id
(`{"tab_id": "...", "workspace_id": "..."}`), so there is nothing to recover
from the event itself. A pane closed inside a tab that survives is the same
problem one level down, and it happens at least as often: Herdr binds
`close_pane` to `prefix+x` and `close_tab` to `prefix+shift+x`, so it is easy
to hit one when you meant the other.

This plugin keeps its own running index of every live tab's layout, refreshed
on the events that can change it, and moves a last known good record onto a
"closed" stack the moment it notices something is gone. Closed tabs and closed
panes share that one stack, ordered by when they died, so the single "reopen"
action always undoes whatever went away last, whichever kind it was.

Restoring a tab replays its record as a new tab: same label, same split tree
with the same ratios, same per-pane working directory and launch command, back
in roughly the same position. Restoring a pane splits it back into the tab it
came from, on the same side of the same split, with the same ratio.

It also brings back what was **running**. A layout-only undo is not much of an
undo when you open a tab with neovim in it, reopen the tab, and neovim is not
there: most tabs hold an editor or a coding agent, and that is the point of the
tab. So an editor comes back on the same file, and a `claude` or `opencode`
pane comes back resumed on the same conversation. Not everything is re-run -
see "What comes back running" for the allowlist and why it exists.

It is still a rebuild, not a live session restore, and the two paths recover
slightly different things, so see "What is restored, and what is not" below for
both exact lines.

## Install

```
herdr plugin install pedroloch/herdr-undo-close
```

For local development, from a checkout of this repository:

```
herdr plugin link .
```

Requires Herdr >= 0.7.0 and `python3` >= 3.9 on PATH.

## Suggested keybindings

Add these to your own `~/.config/herdr/config.toml` (the plugin does not,
and cannot, add keybindings for you - they live in the user's config):

```toml
[[keys.command]]
key = "prefix+shift+t"
type = "plugin_action"
command = "undo-close.reopen-last"
description = "Reopen closed tab or pane"

[[keys.command]]
key = "prefix+shift+u"
type = "plugin_action"
command = "undo-close.pick"
description = "Reopen closed tab or pane from list..."
```

The action ids are unchanged from 0.1.x: if you already bind
`undo-close.reopen-last` and `undo-close.pick`, your config keeps working and
the same keys now also undo a closed pane.

`prefix+shift+t` matches the browser chord (Cmd+Shift+T reopens a closed tab). In a default Herdr install, `prefix+shift+t` is already bound to `rename_tab`, so you must rebind it first - for example to `prefix+n`. Edit `rename_tab` in the `[keys]` table of your config, then add the block above.

If you prefer not to rebind `rename_tab`, use `prefix+t` instead for `undo-close.reopen-last`. It is free in a default install, but it is the browser's "new tab" chord (Cmd+T), so the mnemonic reads slightly wrong.

Before copying into your config, check `~/.config/herdr/config.toml` for collisions with your other keybindings. Run `herdr config check` after editing to validate.

## Right-click menu

Two entries show up wherever their `contexts` allow (global, workspace, tab,
or pane, per action - see below):

- **Reopen closed tab or pane** - restores whichever closed most recently.
- **Reopen closed tab or pane from list...** - opens a small popup with the
  last closed tabs and panes so you can pick one that is not the most recent.
  Each row is tagged `tab` or `pane`; a pane row is named after its own label,
  or its working directory's basename when it has none, followed by the tab it
  lived in.

## Config

Optional, at `$HERDR_PLUGIN_CONFIG_DIR/config.json` (all keys optional,
unknown keys ignored, an invalid value falls back to the default):

```json
{
  "max_entries": 25,
  "focus": true,
  "notify": true,
  "undo_panes": true,
  "restore_process": true,
  "relaunch_agents": true,
  "relaunch_programs": ["nvim", "vim", "vi", "hx", "helix", "emacs", "nano",
                        "kak", "less", "more", "man", "htop", "btop", "top",
                        "tig", "lazygit", "k9s", "watch", "bat"]
}
```

- `max_entries` - how many closed entries to remember (default 25). One budget
  shared by tabs and panes, because they share one stack.
- `focus` - focus the restored tab or pane (default true).
- `notify` - show a Herdr toast on restore and on "nothing to reopen"
  (default true).
- `undo_panes` - record closed panes too (default true). Set it to false to get
  the 0.1.x behavior back: only closed tabs enter the stack, and closing a pane
  merely reshapes its tab's record.
- `restore_process` - master switch for bringing processes back (default true).
  With it off, no pane is ever asked what it is running and every restore is a
  layout-and-cwd restore, exactly like 0.2.x.
- `relaunch_agents` - resume `claude` and `opencode` conversations (default
  true). Agents are their own category and are not filtered by
  `relaunch_programs`.
- `relaunch_programs` - basenames of programs that may be re-run. The default is
  the list above; setting your own **replaces** it, and `[]` disables plain
  program relaunching while leaving agents alone.

## What is restored, and what is not

### Reopening a closed tab

**Restored:** tab label, the split tree and its ratios, each pane's working
directory and launch command, the tab's position among the other tabs in its
workspace, and per pane whatever was running in it that is allowed to be
re-run (see "What comes back running").

**Not restored:** scrollback, zoom state, which pane inside the tab was
focused, and any program outside the allowlist. A pane that was running
something not allowed comes back as a plain shell in that pane's recorded
`cwd`, and the toast says which program that was, so a restore never looks
more complete than it is.

### Reopening a closed pane

A pane is restored into the tab it came from, which has to still be open. The
plugin splits the surviving pane that used to sit next to it, then swaps the
new pane into place when the dead one was on the leading side of that split.

**Restored:** the pane's working directory, its label, its environment, the
split direction, the split ratio, the side of the split it was on, and whatever
was running in it that is allowed to be re-run.

**Not restored:** scrollback, and the original launch command as such. Two
things deserve to be spelled out, because they are worse than the tab path:

- **The launch command is not replayed as a launch command.** Herdr's
  `pane.split` has no `command` parameter, so there is no way to *start* the
  restored pane as anything but a shell. What the plugin can do is type into
  that shell once it is ready, which is how a running program comes back - so a
  pane that was still running its launch command effectively gets it back, and a
  pane whose launch command had already finished does not. When nothing was
  typed, the toast says "launch command was not replayed".
- **The split nesting is approximate when the dead pane's sibling was itself a
  split.** Restoring is one `pane.split` on one surviving pane, which can only
  reproduce a two-way split. If the pane's neighbour was a nested split (say it
  was the left pane facing a stacked pair on the right), the pane comes back
  next to one of those panes rather than facing the pair as a whole. The toast
  says "split position is approximate" in that case, and also when the exact
  neighbour it recorded has since been closed too.

A pane cannot be restored at all once its tab is gone, so a closed-pane entry
is **dropped from the stack** as soon as the plugin notices its tab has closed.
That is not data loss you can act on: with the tab gone there is nothing left
to split. The tab's own entry is usually still there, and reopening it brings
the tab back as it was at the moment it died.

There is deliberately no attempt to reuse `layout.apply` for a pane restore.
Measured against Herdr 0.7.5, `layout.apply` with a `tab_id` is destructive: it
closes the target tab, returns a different `tab_id`, mints all-new pane ids and
loses the surviving panes' scrollback. A `pane_id` inside a layout node does not
mean "reuse this live pane", so there is no non-destructive way to rewrite a tab
that is still open.

## What comes back running

While a pane is alive, every record pass asks Herdr what it is doing
(`pane.process_info`, or `agent.list` when the pane holds an agent) and stores
the answer on the pane's record. It has to happen then: once the pane is closed
its process is dead and nothing can be asked about it. That is also why a pass
runs when Herdr detects an agent in a pane or the agent changes state: starting
`claude` at an existing pane's prompt fires no tab or pane event, so without
those hooks the last record would still say "idle shell" when the pane closes.
On restore that answer turns into one of three outcomes, and the toast always
says which one:

| Outcome | What you see |
| --- | --- |
| brought back | `nvim README.md relaunched`, `claude conversation resumed`, `claude restarted, no conversation to resume` |
| deliberately left alone | `was running psql, not relaunched` |
| tried and failed | `could not relaunch nvim` |

### Why an allowlist

Only programs listed in `relaunch_programs` are re-run. The default list is
editors, pagers and monitors: interactive things where starting a second copy
costs nothing. Everything else is recorded and reported but never executed.

That is not timidity, it is the only safe default. Re-running an arbitrary
command line means re-running whatever was in that pane - a half-finished
database migration, a deploy, `rm -rf` with a bad argument, a `git push`. A tab
you closed by accident is exactly the situation where you least want a command
replayed behind your back. So the plugin tells you what was there and lets you
press up-arrow yourself.

The decision is by basename, so `/usr/local/bin/nvim` is as allowed as `nvim`,
and arguments are never inspected: `nvim` is allowed with any file, `psql` is
refused with any database.

### How an agent is resumed

Agents are a separate category, switched by `relaunch_agents`, because resuming
a conversation is not the same as re-running a command:

- `claude` becomes `claude -r <session-id>`, or plain `claude` when no id is
  known.
- `opencode` becomes `opencode -s <session-id>`, or plain `opencode`.
- any other agent is reported and **not** guessed at. Inventing flags for an
  unknown program is how you end up running something nobody asked for.

Herdr reports a session id for `opencode` (`agent_session`) but not for
`claude`, so the claude id is read off disk instead: `~/.claude/projects/` holds
one directory per working directory, named after the path with every `/` turned
into `-`, and inside it one `<session-id>.jsonl` per conversation. The newest
file wins.

That lookup is a **heuristic** and worth knowing the shape of, because it reads
another tool's private layout:

- if claude ever changes that layout, the lookup quietly stops finding ids and
  every claude pane without a resolvable session id launches plainly (`claude`
  with no flags) instead of resuming anything - a fresh conversation, not a
  broken pane.
- **two claude panes in the same working directory cannot be told apart.** Both
  resolve to the same newest session, so restoring one of them may resume the
  other one's conversation. There is nothing on disk that distinguishes them.

### What can never come back

Scrollback and the original PTY are gone, and always will be. Closing a pane
kills its process; nothing in Herdr's API spares it, and no request accepts a
`terminal_id`, so there is no surviving terminal to re-attach to. A relaunch is
a new process in a new pane - the same program on the same file, not the same
session. What was on screen before is not recoverable by this plugin or by any
other.

## How death is detected

Herdr's `tab.closed` event carries no content, so this plugin does not rely
on it for anything but a same-pass hint. The real detection is
reconciliation: on every relevant event (and on startup), the plugin calls
`tab.list` and compares it against the tabs it is currently tracking. Any
tracked tab that has gone missing from `tab.list` is retired to the closed
stack.

This also covers the one case where Herdr does not even emit `tab.closed`:
closing the *last* pane of a tab destroys the tab outright and emits only
`pane.closed` for that pane, never a `tab.closed` for the tab itself. Since
`pane.closed` is one of this plugin's reconciliation triggers, the very
next pass still notices the tab is gone from `tab.list` and retires it
normally.

Pane death is reconciled the same way, with one extra condition. A pane is
considered dead only when **both** its `pane_id` is missing from `pane.list`
**and** the `terminal_id` recorded for it is missing too. Both halves are
load-bearing: moving a pane to another workspace changes its `pane_id` while
its `terminal_id` follows it (measured: `w15:p24` became `w17:p1`, terminal
`term_657c88fe1054311e` unchanged), so an id-only test would file a bogus undo
entry for every cross-workspace move. Closing a pane for real removes the
terminal from `pane.list` as well. Unlike an event-ordering heuristic, this
test does not care which event arrived first, or whether one was missed.

The cost is one blind event: a pane whose record predates this version has no
`terminal_id` yet, and a pane with no recorded terminal is never treated as
dead, because a phantom entry is worse than a missed one. The next pass has the
field.

When a pane and its whole tab die in the same pass, only the tab entry is
recorded - it already contains the pane. When both happen in the same pass for
*different* tabs, pane entries are stacked ahead of tab entries, since a pass
stamps every death with the same timestamp and the pane is the more surprising
loss to offer first.

## Troubleshooting

```
herdr plugin log list --plugin undo-close
```

The `record` subcommand exits non-zero with a one-line stderr message
whenever the socket is unreachable or a request to Herdr errors, so a
silent gap in the recorded history should show up there.

## Roadmap

Starting a restored pane directly with its command, once Herdr offers a way to
do it without going through `layout.apply` on a whole tab. Typing into the new
pane's shell works - the plugin waits for the shell to be idle before sending
anything, rather than sleeping and hoping - but it is a workaround: the command
lands in shell history, and a shell that never becomes ready means no relaunch
at all. A `command` parameter on `pane.split`, which `layout.apply`'s tree
already has, would remove the whole mechanism.

## Development

```
cd herdr-undo-close
python3 -m unittest discover -s tests
python3 -m compileall -q herdr_undo_close tests
```

Standard library only, Python 3.9-compatible on purpose (no `tomllib`, no
`match`, no `X | Y` unions at runtime): this has to run on whatever
`python3` Herdr finds on the user's machine, not on a pinned toolchain.
