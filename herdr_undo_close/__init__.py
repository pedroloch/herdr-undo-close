"""herdr-undo-close: reopen a closed Herdr tab or pane, Chrome-style.

Standard-library-only, Python 3.9-compatible plugin for Herdr. Closed tabs
and closed panes share one stack ordered by death time, so a single action
undoes whichever died last, and a restore brings back the editor or agent
that was running in it, not just the layout. See README.md for what it does
and how it is wired up; see the docstrings in each module for the mechanics.
"""

__version__ = "0.3.0"
