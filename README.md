# herdr-undo-close

Reopen a closed tab in Herdr, the way Cmd+Shift+T reopens a closed tab in a
browser.

## What it does, and why

Herdr does not have an undo-close. Once a tab is gone, its split layout,
per-pane working directories, and launch commands are gone with it: the
`tab.closed` event Herdr fires carries no tab content at all, just an id
(`{"tab_id": "...", "workspace_id": "..."}`), so there is nothing to recover
from the event itself.

This plugin keeps its own running index of every live tab's layout,
refreshed on the events that can change it, and moves a tab's last known
good record onto a "closed" stack the moment it notices the tab is gone.
"Reopen closed tab" then replays that record: same label, same split tree
with the same ratios, same per-pane working directory and launch command,
back in roughly the same position. It is a faithful shell-level rebuild,
not a live session restore - see "What is restored" below for the exact
line.

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
description = "Reopen closed tab"

[[keys.command]]
key = "prefix+shift+u"
type = "plugin_action"
command = "undo-close.pick"
description = "Reopen closed tab from list..."
```

`prefix+shift+t` matches the browser chord (Cmd+Shift+T reopens a closed tab). In a default Herdr install, `prefix+shift+t` is already bound to `rename_tab`, so you must rebind it first - for example to `prefix+n`. Edit `rename_tab` in the `[keys]` table of your config, then add the block above.

If you prefer not to rebind `rename_tab`, use `prefix+t` instead for `undo-close.reopen-last`. It is free in a default install, but it is the browser's "new tab" chord (Cmd+T), so the mnemonic reads slightly wrong.

Before copying into your config, check `~/.config/herdr/config.toml` for collisions with your other keybindings. Run `herdr config check` after editing to validate.

## Right-click menu

Two entries show up wherever their `contexts` allow (global, workspace, tab,
or pane, per action - see below):

- **Reopen closed tab** - restores the single most recently closed tab.
- **Reopen closed tab from list...** - opens a small popup with the last
  closed tabs so you can pick one that is not the most recent.

## Config

Optional, at `$HERDR_PLUGIN_CONFIG_DIR/config.json` (all keys optional,
unknown keys ignored, an invalid value falls back to the default):

```json
{
  "max_entries": 25,
  "focus": true,
  "notify": true
}
```

- `max_entries` - how many closed tabs to remember (default 25).
- `focus` - focus the restored tab (default true).
- `notify` - show a Herdr toast on restore and on "nothing to reopen"
  (default true).

## What is restored, and what is not

**Restored:** tab label, the split tree and its ratios, each pane's working
directory and launch command, and the tab's position among the other tabs
in its workspace.

**Not restored:** running processes, agent conversations, scrollback, zoom
state, and which pane inside the tab was focused. A pane that was running
an agent (say `claude`) comes back as a plain shell in that pane's recorded
`cwd` - the agent is not restarted. This is deliberate, documented
behavior, not a bug: restarting an arbitrary process is out of scope for a
layout-restore plugin, and the toast on restore says so explicitly when it
applies.

**Scope boundary: tabs, not panes.** This plugin undoes closed tabs, not closed panes. Herdr's default keybinding for `close_pane` is `prefix+x` and `close_tab` is `prefix+shift+x`, so it is easy to close one when you meant the other. Closing a single pane within a multi-pane tab records the tab's new layout but does not create an undoable event - the next time that tab is reopened, it restores as it was at the moment the tab closed, without the pane you deliberately removed. Closing the last remaining pane of a tab does destroy the tab outright, and that is undoable via this plugin. Restoring a closed pane back into its original split position and tab is not currently supported; only closed tabs can be reopened.

## How tab death is detected

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

## Troubleshooting

```
herdr plugin log list --plugin undo-close
```

The `record` subcommand exits non-zero with a one-line stderr message
whenever the socket is unreachable or a request to Herdr errors, so a
silent gap in the recorded history should show up there.

## Roadmap

The next planned step is to undo a closed pane, not just a closed tab.
Herdr's default keybindings are `prefix+x` for `close_pane` and `prefix+shift+x`
for `close_tab`, so a pane closed by accident is at least as common as a tab.
Today, closing a pane within a surviving tab is not undoable - it becomes that
tab's new layout. The plan: when the pane's tab still exists, restore the pane
at its original split position, direction, and ratio; when the tab is gone too,
fall back to the current whole-tab restore. This requires a single undo stack
ordered by death time, so one action undoes whatever died last - pane or tab.

## Development

```
cd herdr-undo-close
python3 -m unittest discover -s tests
python3 -m compileall -q herdr_undo_close tests
```

Standard library only, Python 3.9-compatible on purpose (no `tomllib`, no
`match`, no `X | Y` unions at runtime): this has to run on whatever
`python3` Herdr finds on the user's machine, not on a pinned toolchain.
