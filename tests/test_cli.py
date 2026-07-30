"""Tests for herdr_undo_close.cli's reporting: a restore must never look
cleaner than it was. Every lossy part of a pane restore has to reach the
toast body and the stdout line, so these assert on the strings themselves.
Pure functions only - no sockets, no state directory."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from herdr_undo_close import cli


def tab_result(**overrides):
    result = {
        "kind": "tab",
        "new_tab_id": "w1:new",
        "workspace_id": "w1",
        "fallback_used": False,
        "position_restored": True,
        "pane_count": 2,
        "agent_kinds": [],
        "label": "probe-3",
    }
    result.update(overrides)
    return result


def pane_result(**overrides):
    result = {
        "kind": "pane",
        "new_pane_id": "w1:p9",
        "tab_id": "w1:t7",
        "tab_label": "probe-3",
        "label": "build",
        "cwd": "/home/pedro/proj",
        "exact": True,
        "agent": None,
        "command_lost": False,
        "relaunched": [],
        "not_relaunched": [],
        "relaunch_failed": [],
    }
    result.update(overrides)
    return result


def process_plan(program="nvim", display="nvim README.md", allowed=True):
    return {
        "kind": "process",
        "program": program,
        "display": display,
        "command": [program] if allowed else None,
        "allowed": allowed,
    }


def agent_plan(program="claude", allowed=True, session_id="ses_1"):
    return {
        "kind": "agent",
        "program": program,
        "display": program,
        "command": ([program, "-r", session_id] if session_id else [program]) if allowed else None,
        "allowed": allowed,
        "session_id": session_id,
    }


class TestTabReporting(unittest.TestCase):
    def test_clean_restore_only_reports_the_pane_count(self):
        self.assertEqual(cli._summary_body(tab_result()), "2 pane(s)")

    def test_caveats_are_listed(self):
        body = cli._summary_body(
            tab_result(agent_kinds=["claude"], fallback_used=True, position_restored=False)
        )
        self.assertIn("claude was not restarted", body)
        self.assertIn("original workspace was gone", body)
        self.assertIn("position could not be restored", body)

    def test_line_names_the_new_tab(self):
        self.assertIn("as w1:new", cli._summary_line(tab_result()))


class TestPaneReporting(unittest.TestCase):
    def test_exact_restore_only_names_the_tab(self):
        self.assertEqual(cli._summary_body(pane_result()), "in tab 'probe-3'")

    def test_approximate_split_is_reported(self):
        self.assertIn("split position is approximate", cli._summary_body(pane_result(exact=False)))

    def test_agent_not_restarted_is_reported(self):
        self.assertIn("claude was not restarted", cli._summary_body(pane_result(agent="claude")))

    def test_lost_launch_command_is_reported(self):
        # pane.split has no command parameter, so this is a real regression
        # against the tab path and must be said out loud.
        self.assertIn(
            "launch command was not replayed", cli._summary_body(pane_result(command_lost=True))
        )

    def test_line_and_title_name_the_pane_and_the_new_id(self):
        line = cli._summary_line(pane_result())
        self.assertIn("reopened pane 'build'", line)
        self.assertIn("as w1:p9", line)
        self.assertEqual(cli._summary_title(pane_result()), "Reopened pane build")

    def test_unlabelled_pane_is_named_after_its_cwd(self):
        self.assertIn("reopened pane 'proj'", cli._summary_line(pane_result(label="")))


class TestRelaunchReporting(unittest.TestCase):
    """The three outcomes must be distinguishable in the toast: brought back,
    deliberately left alone, or tried and failed."""

    def test_a_relaunched_program_is_named_with_its_arguments(self):
        body = cli._summary_body(pane_result(relaunched=[process_plan()]))
        self.assertIn("nvim README.md relaunched", body)

    def test_a_resumed_agent_reads_as_a_conversation(self):
        body = cli._summary_body(pane_result(relaunched=[agent_plan()]))
        self.assertIn("claude conversation resumed", body)
        # And the pane path has already dropped the contradicting caveat.
        self.assertNotIn("was not restarted", body)

    def test_a_deliberately_skipped_program_says_what_it_was(self):
        body = cli._summary_body(
            pane_result(not_relaunched=[process_plan("psql", "psql prod", allowed=False)])
        )
        self.assertIn("was running psql, not relaunched", body)

    def test_a_failed_relaunch_is_admitted(self):
        body = cli._summary_body(pane_result(relaunch_failed=[process_plan()]))
        self.assertIn("could not relaunch nvim", body)

    def test_a_tab_reports_relaunches_too(self):
        body = cli._summary_body(
            tab_result(
                relaunched=[agent_plan()],
                not_relaunched=[process_plan("psql", "psql prod", allowed=False)],
                relaunch_failed=[],
            )
        )
        self.assertIn("claude conversation resumed", body)
        self.assertIn("was running psql, not relaunched", body)

    def test_identical_phrases_are_said_once(self):
        body = cli._summary_body(
            tab_result(relaunched=[process_plan(), process_plan()], not_relaunched=[])
        )
        self.assertEqual(body.count("nvim README.md relaunched"), 1)

    def test_a_result_without_the_relaunch_keys_still_renders(self):
        # A restore recorded by an older version of the plugin.
        legacy = pane_result()
        for key in ("relaunched", "not_relaunched", "relaunch_failed"):
            del legacy[key]
        self.assertEqual(cli._summary_body(legacy), "in tab 'probe-3'")


if __name__ == "__main__":
    unittest.main()
