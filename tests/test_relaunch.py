"""Tests for herdr_undo_close.relaunch: working out what a pane was running,
resolving a claude session id from a FAKE ~/.claude tree, the allowlist that
decides what may be re-run, and the readiness polling that replaces a fixed
sleep. A fake API stands in for the socket, and claude_projects_dir is always
pointed at a temp directory - no test reads the real ~/.claude."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from herdr_undo_close import config as config_mod
from herdr_undo_close import relaunch

CONFIG = {
    "restore_process": True,
    "relaunch_agents": True,
    "relaunch_programs": ["nvim", "less"],
}


def config(**overrides):
    result = dict(CONFIG)
    result.update(overrides)
    return result


class FakeApi(object):
    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        answer = self.script.get(method, {})
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, type) and issubclass(answer, Exception):
            raise answer("fake error")
        if callable(answer):
            return answer(params)
        return answer


class FakeClaudeHome(unittest.TestCase):
    """Every test in this module points the claude lookup at a temp tree."""

    def setUp(self):
        super(FakeClaudeHome, self).setUp()
        self.projects = tempfile.mkdtemp(prefix="herdr-undo-close-claude-")
        self.addCleanup(shutil.rmtree, self.projects, True)
        patcher = mock.patch.object(
            relaunch, "claude_projects_dir", return_value=self.projects
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def plant_session(self, cwd, session_id, mtime):
        directory = os.path.join(self.projects, relaunch.project_slug(cwd))
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, session_id + ".jsonl")
        with open(path, "w") as f:
            f.write('{"type":"user"}\n')
        os.utime(path, (mtime, mtime))
        return path


class TestProjectSlug(unittest.TestCase):
    def test_every_slash_becomes_a_dash(self):
        self.assertEqual(
            relaunch.project_slug("/private/tmp/x/resume-test"),
            "-private-tmp-x-resume-test",
        )

    def test_existing_dashes_are_left_alone(self):
        self.assertEqual(relaunch.project_slug("/Users/pedro/my-repo"), "-Users-pedro-my-repo")

    def test_dots_become_dashes(self):
        self.assertEqual(relaunch.project_slug("/home/u/.cache/repro"), "-home-u--cache-repro")


class TestClaudeSessionInCwd(FakeClaudeHome):
    def test_true_only_for_a_log_under_the_cwd(self):
        self.plant_session("/proj", "mine", 1)
        self.plant_session("/elsewhere", "nested", 1)
        self.assertTrue(relaunch.claude_session_in_cwd("mine", "/proj"))
        self.assertFalse(relaunch.claude_session_in_cwd("nested", "/proj"))
        self.assertFalse(relaunch.claude_session_in_cwd(None, "/proj"))
        self.assertFalse(relaunch.claude_session_in_cwd("mine", None))


class TestFindClaudeSessionId(FakeClaudeHome):
    def test_finds_the_session_for_the_right_slug(self):
        self.plant_session("/proj", "3323d1b5-617d-49ce-b2ed-486fddb0e400", mtime=1000)
        self.assertEqual(
            relaunch.find_claude_session_id("/proj"),
            "3323d1b5-617d-49ce-b2ed-486fddb0e400",
        )

    def test_newest_file_wins(self):
        self.plant_session("/proj", "old-session", mtime=1000)
        self.plant_session("/proj", "new-session", mtime=2000)
        self.assertEqual(relaunch.find_claude_session_id("/proj"), "new-session")

    def test_another_directorys_sessions_are_not_borrowed(self):
        self.plant_session("/other", "other-session", mtime=1000)
        self.assertIsNone(relaunch.find_claude_session_id("/proj"))

    def test_missing_directory_yields_none(self):
        self.assertIsNone(relaunch.find_claude_session_id("/never-used"))

    def test_directory_without_jsonl_files_yields_none(self):
        directory = os.path.join(self.projects, relaunch.project_slug("/proj"))
        os.makedirs(directory)
        with open(os.path.join(directory, "notes.txt"), "w") as f:
            f.write("x")
        self.assertIsNone(relaunch.find_claude_session_id("/proj"))

    def test_no_cwd_yields_none_without_looking(self):
        self.assertIsNone(relaunch.find_claude_session_id(None))
        self.assertIsNone(relaunch.find_claude_session_id(""))


class TestBuildAgentIntent(FakeClaudeHome):
    def test_uses_the_session_id_herdr_reported(self):
        pane = {
            "pane_id": "pA",
            "cwd": "/proj",
            "agent": "opencode",
            "agent_session": {
                "agent": "opencode",
                "kind": "id",
                "source": "herdr:opencode",
                "value": "ses_04fe18399ffe4iiO36W7CaQigc",
            },
        }
        self.assertEqual(
            relaunch.build_agent_intent(pane),
            {
                "kind": "agent",
                "agent": "opencode",
                "session_id": "ses_04fe18399ffe4iiO36W7CaQigc",
            },
        )

    def test_a_path_session_is_used_for_pi(self):
        pane = {
            "pane_id": "pA",
            "cwd": "/proj",
            "agent": "pi",
            "agent_session": {"agent": "pi", "kind": "path", "value": "/s/a.jsonl"},
        }
        self.assertEqual(
            relaunch.build_agent_intent(pane),
            {"kind": "agent", "agent": "pi", "session_id": "/s/a.jsonl"},
        )

    def test_a_path_session_is_ignored_for_an_agent_that_takes_ids(self):
        pane = {
            "pane_id": "pA",
            "cwd": "/proj",
            "agent": "codex",
            "agent_session": {"agent": "codex", "kind": "path", "value": "/s/a.jsonl"},
        }
        self.assertEqual(
            relaunch.build_agent_intent(pane),
            {"kind": "agent", "agent": "codex", "session_id": None},
        )

    def test_claude_without_a_reported_session_falls_back_to_disk(self):
        # agent.list reports agent_session for opencode but not for claude.
        self.plant_session("/proj", "disk-session", mtime=1000)
        pane = {"pane_id": "pA", "cwd": "/proj", "agent": "claude"}
        self.assertEqual(
            relaunch.build_agent_intent(pane),
            {"kind": "agent", "agent": "claude", "session_id": "disk-session"},
        )

    def test_claude_with_nothing_on_disk_is_not_an_error(self):
        pane = {"pane_id": "pA", "cwd": "/proj", "agent": "claude"}
        self.assertEqual(
            relaunch.build_agent_intent(pane),
            {"kind": "agent", "agent": "claude", "session_id": None},
        )

    def test_an_unknown_agent_still_produces_an_intent(self):
        pane = {"pane_id": "pA", "cwd": "/proj", "agent": "aider"}
        self.assertEqual(
            relaunch.build_agent_intent(pane),
            {"kind": "agent", "agent": "aider", "session_id": None},
        )

    def test_no_agent_means_no_agent_intent(self):
        self.assertIsNone(relaunch.build_agent_intent({"pane_id": "pA", "cwd": "/proj"}))

    def test_a_path_session_ref_is_not_treated_as_an_id(self):
        pane = {
            "pane_id": "pA",
            "cwd": "/proj",
            "agent": "opencode",
            "agent_session": {"kind": "path", "value": "/somewhere/session.json"},
        }
        self.assertEqual(relaunch.build_agent_intent(pane)["session_id"], None)


class TestBuildProcessIntent(unittest.TestCase):
    def test_foreground_process_other_than_the_shell(self):
        info = {
            "shell_pid": 3135,
            "foreground_process_group_id": 3716,
            "foreground_processes": [
                {
                    "pid": 3716,
                    "name": "nvim",
                    "argv0": "nvim",
                    "argv": ["nvim", "README.md"],
                    "cmdline": "nvim README.md",
                    "cwd": "/Users/pedro/maestro",
                }
            ],
        }
        self.assertEqual(
            relaunch.build_process_intent(info),
            {"kind": "process", "argv": ["nvim", "README.md"]},
        )

    def test_an_idle_shell_yields_nothing(self):
        info = {
            "shell_pid": 3135,
            "foreground_processes": [{"pid": 3135, "name": "-zsh", "argv": ["-zsh"]}],
        }
        self.assertIsNone(relaunch.build_process_intent(info))

    def test_no_foreground_processes_at_all_yields_nothing(self):
        self.assertIsNone(relaunch.build_process_intent({"shell_pid": 1, "foreground_processes": []}))

    def test_a_process_without_argv_yields_nothing(self):
        info = {"shell_pid": 1, "foreground_processes": [{"pid": 2, "name": "nvim"}]}
        self.assertIsNone(relaunch.build_process_intent(info))

    def test_a_missing_shell_pid_yields_nothing_rather_than_a_guess(self):
        info = {"foreground_processes": [{"pid": 2, "name": "-zsh", "argv": ["-zsh"]}]}
        self.assertIsNone(relaunch.build_process_intent(info))

    def test_a_non_dict_answer_yields_nothing(self):
        self.assertIsNone(relaunch.build_process_intent(None))


class TestBuildPaneIntent(FakeClaudeHome):
    def test_an_agent_pane_never_asks_for_process_info(self):
        api = FakeApi()
        pane = {
            "pane_id": "pA",
            "cwd": "/proj",
            "agent": "opencode",
            "agent_session": {"kind": "id", "value": "ses_1"},
        }
        intent = relaunch.build_pane_intent(api, pane)
        self.assertEqual(intent["session_id"], "ses_1")
        self.assertEqual(api.calls, [])

    def test_a_plain_pane_asks_once_and_uses_the_answer(self):
        api = FakeApi(
            {
                "pane.process_info": {
                    "type": "pane_process_info",
                    "process_info": {
                        "shell_pid": 10,
                        "foreground_processes": [
                            {"pid": 11, "name": "vim", "argv": ["vim", "a.txt"]}
                        ],
                    },
                }
            }
        )
        intent = relaunch.build_pane_intent(api, {"pane_id": "pA", "cwd": "/proj"})
        self.assertEqual(intent, {"kind": "process", "argv": ["vim", "a.txt"]})
        self.assertEqual(api.calls, [("pane.process_info", {"pane_id": "pA"})])

    def test_a_failing_process_info_yields_no_intent_and_no_exception(self):
        api = FakeApi({"pane.process_info": RuntimeError("no such method")})
        self.assertIsNone(relaunch.build_pane_intent(api, {"pane_id": "pA"}))

    def test_a_pane_without_an_id_is_not_queried(self):
        api = FakeApi()
        self.assertIsNone(relaunch.build_pane_intent(api, {"cwd": "/proj"}))
        self.assertEqual(api.calls, [])


class TestAttachIntents(FakeClaudeHome):
    def test_only_panes_with_something_running_get_a_relaunch_key(self):
        api = FakeApi(
            {
                "pane.process_info": lambda params: {
                    "type": "pane_process_info",
                    "process_info": {
                        "shell_pid": 10,
                        "foreground_processes": [
                            {"pid": 11, "name": "nvim", "argv": ["nvim"]}
                            if params["pane_id"] == "pA"
                            else {"pid": 10, "name": "-zsh", "argv": ["-zsh"]}
                        ],
                    },
                }
            }
        )
        panes = [{"pane_id": "pA"}, {"pane_id": "pB"}]
        relaunch.attach_intents(api, panes)
        self.assertEqual(panes[0]["relaunch"], {"kind": "process", "argv": ["nvim"]})
        self.assertNotIn("relaunch", panes[1])


class TestAgentCommand(unittest.TestCase):
    def test_claude_with_and_without_an_id(self):
        self.assertEqual(relaunch.agent_command("claude", "abc"), ["claude", "-r", "abc"])
        self.assertEqual(relaunch.agent_command("claude", None), ["claude"])

    def test_opencode_with_and_without_an_id(self):
        self.assertEqual(relaunch.agent_command("opencode", "ses_1"), ["opencode", "-s", "ses_1"])
        self.assertEqual(relaunch.agent_command("opencode", None), ["opencode"])

    def test_agents_herdr_knows_how_to_resume(self):
        self.assertEqual(relaunch.agent_command("codex", "c1"), ["codex", "resume", "c1"])
        self.assertEqual(relaunch.agent_command("codex", None), ["codex"])
        self.assertEqual(relaunch.agent_command("omp", "o1"), ["omp", "--resume=o1"])
        self.assertEqual(relaunch.agent_command("copilot", "p1"), ["copilot", "--resume=p1"])
        self.assertEqual(relaunch.agent_command("cursor", "u1"), ["cursor-agent", "--resume", "u1"])
        self.assertEqual(relaunch.agent_command("cursor", None), ["cursor-agent"])
        self.assertEqual(
            relaunch.agent_command("pi", "/s/a.jsonl"), ["pi", "--session", "/s/a.jsonl"]
        )

    def test_letta_default_conversation_is_addressed_by_agent_id(self):
        self.assertEqual(
            relaunch.agent_command("letta", "default:agent-1"),
            ["letta", "--conversation", "default", "--agent", "agent-1"],
        )
        self.assertEqual(
            relaunch.agent_command("letta", "conv-1"), ["letta", "--conversation", "conv-1"]
        )

    def test_an_unknown_agent_is_never_guessed_at(self):
        self.assertIsNone(relaunch.agent_command("aider", "abc"))
        self.assertIsNone(relaunch.agent_command(None, None))


class TestRelaunchPlan(unittest.TestCase):
    def test_an_allowed_program_is_relaunched(self):
        plan = relaunch.relaunch_plan(
            {"kind": "process", "argv": ["nvim", "README.md"]}, CONFIG
        )
        self.assertTrue(plan["allowed"])
        self.assertEqual(plan["command"], ["nvim", "README.md"])
        self.assertEqual(plan["program"], "nvim")
        self.assertEqual(plan["display"], "nvim README.md")

    def test_the_decision_is_by_basename(self):
        plan = relaunch.relaunch_plan(
            {"kind": "process", "argv": ["/usr/bin/nvim", "README.md"]}, CONFIG
        )
        self.assertTrue(plan["allowed"])
        self.assertEqual(plan["program"], "nvim")

    def test_a_program_outside_the_allowlist_is_recorded_but_not_run(self):
        plan = relaunch.relaunch_plan({"kind": "process", "argv": ["psql", "prod"]}, CONFIG)
        self.assertFalse(plan["allowed"])
        self.assertIsNone(plan["command"])
        self.assertEqual(plan["program"], "psql")

    def test_the_shipped_allowlist_holds_editors_and_not_a_database_shell(self):
        programs = config_mod.DEFAULTS["relaunch_programs"]
        self.assertIn("nvim", programs)
        self.assertIn("htop", programs)
        self.assertNotIn("psql", programs)

    def test_an_agent_becomes_a_resume_command(self):
        plan = relaunch.relaunch_plan(
            {"kind": "agent", "agent": "claude", "session_id": "abc"}, CONFIG
        )
        self.assertTrue(plan["allowed"])
        self.assertEqual(plan["command"], ["claude", "-r", "abc"])

    def test_an_agent_is_not_filtered_by_the_program_allowlist(self):
        plan = relaunch.relaunch_plan(
            {"kind": "agent", "agent": "claude", "session_id": None},
            config(relaunch_programs=[]),
        )
        self.assertTrue(plan["allowed"])

    def test_an_unknown_agent_is_reported_not_guessed(self):
        plan = relaunch.relaunch_plan(
            {"kind": "agent", "agent": "aider", "session_id": None}, CONFIG
        )
        self.assertFalse(plan["allowed"])
        self.assertIsNone(plan["command"])
        self.assertEqual(plan["program"], "aider")

    def test_restore_process_false_suppresses_everything(self):
        cfg = config(restore_process=False)
        self.assertIsNone(
            relaunch.relaunch_plan({"kind": "process", "argv": ["nvim"]}, cfg)
        )
        self.assertIsNone(
            relaunch.relaunch_plan({"kind": "agent", "agent": "claude", "session_id": None}, cfg)
        )

    def test_relaunch_agents_false_suppresses_only_agents(self):
        cfg = config(relaunch_agents=False)
        self.assertIsNone(
            relaunch.relaunch_plan({"kind": "agent", "agent": "claude", "session_id": None}, cfg)
        )
        self.assertTrue(relaunch.relaunch_plan({"kind": "process", "argv": ["nvim"]}, cfg))

    def test_no_intent_means_no_plan(self):
        self.assertIsNone(relaunch.relaunch_plan(None, CONFIG))
        self.assertIsNone(relaunch.relaunch_plan({"kind": "process", "argv": []}, CONFIG))
        self.assertIsNone(relaunch.relaunch_plan({"kind": "nonsense"}, CONFIG))


class TestDescriptions(unittest.TestCase):
    def test_relaunched_process_names_the_whole_command_line(self):
        plan = relaunch.relaunch_plan(
            {"kind": "process", "argv": ["nvim", "README.md"]}, CONFIG
        )
        self.assertEqual(relaunch.describe_relaunched(plan), "nvim README.md relaunched")

    def test_a_resumed_agent_reads_as_a_conversation(self):
        plan = relaunch.relaunch_plan(
            {"kind": "agent", "agent": "claude", "session_id": "abc"}, CONFIG
        )
        self.assertEqual(relaunch.describe_relaunched(plan), "claude conversation resumed")

    def test_a_claude_agent_with_no_session_reads_as_honestly_restarted(self):
        plan = relaunch.relaunch_plan(
            {"kind": "agent", "agent": "claude", "session_id": None}, CONFIG
        )
        self.assertEqual(plan["command"], ["claude"])
        self.assertEqual(
            relaunch.describe_relaunched(plan), "claude restarted, no conversation to resume"
        )

    def test_an_opencode_agent_with_no_session_reads_as_honestly_restarted(self):
        plan = relaunch.relaunch_plan(
            {"kind": "agent", "agent": "opencode", "session_id": None}, CONFIG
        )
        self.assertEqual(plan["command"], ["opencode"])
        self.assertEqual(
            relaunch.describe_relaunched(plan), "opencode restarted, no conversation to resume"
        )

    def test_a_skipped_program_says_what_it_was(self):
        plan = relaunch.relaunch_plan({"kind": "process", "argv": ["psql", "prod"]}, CONFIG)
        self.assertEqual(
            relaunch.describe_skipped(plan), "was running psql, not relaunched"
        )

    def test_a_failed_relaunch_names_the_program(self):
        plan = relaunch.relaunch_plan({"kind": "process", "argv": ["nvim"]}, CONFIG)
        self.assertEqual(relaunch.describe_failed(plan), "could not relaunch nvim")


class TestShellIsReady(unittest.TestCase):
    def test_a_shell_at_its_prompt_is_ready(self):
        self.assertTrue(
            relaunch.shell_is_ready(
                {"shell_pid": 10, "foreground_processes": [{"pid": 10, "name": "-zsh"}]}
            )
        )

    def test_no_foreground_process_is_ready(self):
        self.assertTrue(relaunch.shell_is_ready({"shell_pid": 10, "foreground_processes": []}))

    def test_a_running_program_is_not_ready(self):
        self.assertFalse(
            relaunch.shell_is_ready(
                {"shell_pid": 10, "foreground_processes": [{"pid": 11, "name": "nvim"}]}
            )
        )

    def test_no_shell_pid_yet_is_not_ready(self):
        self.assertFalse(relaunch.shell_is_ready({"foreground_processes": []}))
        self.assertFalse(relaunch.shell_is_ready({}))
        self.assertFalse(relaunch.shell_is_ready(None))


class TestWaitForShell(unittest.TestCase):
    """The point of polling is that a fixed sleep loses keystrokes to a slow
    shell profile, so what matters is that it keeps asking and that it gives
    up instead of typing into a pane that never came up."""

    def _clock(self, ticks):
        state = {"i": 0}

        def clock():
            i = min(state["i"], len(ticks) - 1)
            state["i"] += 1
            return ticks[i]

        return clock

    def test_returns_true_as_soon_as_the_shell_is_idle(self):
        answers = [
            {
                "type": "pane_process_info",
                "process_info": {
                    "shell_pid": 10,
                    "foreground_processes": [{"pid": 11, "name": "zsh-startup"}],
                },
            },
            {
                "type": "pane_process_info",
                "process_info": {
                    "shell_pid": 10,
                    "foreground_processes": [{"pid": 10, "name": "-zsh"}],
                },
            },
        ]
        api = FakeApi({"pane.process_info": lambda params: answers.pop(0)})
        slept = []
        self.assertTrue(
            relaunch.wait_for_shell(
                api, "w1:p9", timeout_s=10.0, interval_s=0.2, sleep=slept.append,
                clock=self._clock([0.0, 0.2, 0.4, 0.6]),
            )
        )
        self.assertEqual(len(api.calls), 2)
        self.assertEqual(slept, [0.2])

    def test_gives_up_at_the_timeout_without_raising(self):
        api = FakeApi(
            {
                "pane.process_info": {
                    "type": "pane_process_info",
                    "process_info": {
                        "shell_pid": 10,
                        "foreground_processes": [{"pid": 11}],
                    },
                }
            }
        )
        slept = []
        self.assertFalse(
            relaunch.wait_for_shell(
                api, "w1:p9", timeout_s=1.0, interval_s=0.2, sleep=slept.append,
                clock=self._clock([0.0, 0.5, 1.5]),
            )
        )
        self.assertEqual(len(api.calls), 2)

    def test_a_failing_query_counts_as_not_ready_yet(self):
        api = FakeApi({"pane.process_info": RuntimeError("pane not answering")})
        self.assertFalse(
            relaunch.wait_for_shell(
                api, "w1:p9", timeout_s=0.0, interval_s=0.0, sleep=lambda s: None,
                clock=self._clock([0.0, 0.0]),
            )
        )


class TestRealSocketEnvelope(unittest.TestCase):
    """Regression test for the bug where the WHOLE pane.process_info result
    was read as if it were the inner payload. The real socket nests the
    payload one level deeper, under "process_info", next to a "type" key
    (measured live: a pane running nvim recorded `relaunch: null` because
    `info.get("shell_pid")` was always None on the envelope). Every field
    here is copied faithfully from that live response, extras included, so
    unwrapping cannot pass by accident just because a fixture was minimal.
    """

    def _clock(self, ticks):
        state = {"i": 0}

        def clock():
            i = min(state["i"], len(ticks) - 1)
            state["i"] += 1
            return ticks[i]

        return clock

    def test_build_pane_intent_unwraps_the_real_envelope(self):
        envelope = {
            "type": "pane_process_info",
            "process_info": {
                "pane_id": "w15:p3A",
                "shell_pid": 3135,
                "foreground_process_group_id": 3716,
                "foreground_processes": [
                    {
                        "pid": 3716,
                        "name": "nvim",
                        "argv0": "nvim",
                        "argv": ["nvim", "README.md"],
                        "cmdline": "nvim README.md",
                        "cwd": "/Users/pedro/maestro",
                    }
                ],
            },
        }
        api = FakeApi({"pane.process_info": envelope})
        intent = relaunch.build_pane_intent(api, {"pane_id": "w15:p3A"})
        self.assertEqual(intent, {"kind": "process", "argv": ["nvim", "README.md"]})

    def test_wait_for_shell_recognizes_an_idle_shell_from_the_real_envelope(self):
        envelope = {
            "type": "pane_process_info",
            "process_info": {
                "pane_id": "w15:p3A",
                "shell_pid": 3135,
                "foreground_process_group_id": 3135,
                "foreground_processes": [
                    {
                        "pid": 3135,
                        "name": "-zsh",
                        "argv0": "-zsh",
                        "argv": ["-zsh"],
                        "cmdline": "-zsh",
                        "cwd": "/Users/pedro/maestro",
                    }
                ],
            },
        }
        api = FakeApi({"pane.process_info": lambda params: envelope})
        slept = []
        ready = relaunch.wait_for_shell(
            api, "w15:p3A", timeout_s=10.0, interval_s=0.2, sleep=slept.append,
            clock=self._clock([0.0, 0.2, 10.0]),
        )
        self.assertTrue(ready)
        # Ready on the very first poll: no sleep needed. The unwrap bug made
        # this always False, so wait_for_shell burned the full 10s timeout
        # instead - the opposite of "promptly".
        self.assertEqual(slept, [])
        self.assertEqual(len(api.calls), 1)


class TestCommandLine(unittest.TestCase):
    def test_plain_arguments_are_left_readable(self):
        self.assertEqual(relaunch.command_line(["nvim", "README.md"]), "nvim README.md")

    def test_a_space_is_quoted(self):
        self.assertEqual(relaunch.command_line(["nvim", "my notes.md"]), "nvim 'my notes.md'")

    def test_shell_metacharacters_are_quoted(self):
        self.assertEqual(
            relaunch.command_line(["less", "a; rm -rf b"]), "less 'a; rm -rf b'"
        )

    def test_send_command_ends_with_a_newline(self):
        api = FakeApi({"pane.send_text": {}})
        relaunch.send_command(api, "w1:p9", ["nvim", "README.md"])
        self.assertEqual(
            api.calls, [("pane.send_text", {"pane_id": "w1:p9", "text": "nvim README.md\n"})]
        )


if __name__ == "__main__":
    unittest.main()
