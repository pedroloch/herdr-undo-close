"""Tests for herdr_undo_close.config: a corrupt or wrongly typed config.json
must fall back to defaults rather than crash a hook, and the relaunch
allowlist has to be a list of strings by the time anything reads it - a stray
number in there would reach os.path.basename during a restore."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from herdr_undo_close import config as config_mod


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.config_dir = tempfile.mkdtemp(prefix="herdr-undo-close-test-")
        self.addCleanup(shutil.rmtree, self.config_dir, True)

    def write(self, raw):
        with open(os.path.join(self.config_dir, "config.json"), "w") as f:
            f.write(raw if isinstance(raw, str) else json.dumps(raw))

    def load(self):
        return config_mod.load_config(self.config_dir)


class TestDefaults(ConfigTestCase):
    def test_no_file_yields_every_default(self):
        cfg = self.load()
        self.assertTrue(cfg["restore_process"])
        self.assertTrue(cfg["relaunch_agents"])
        self.assertIn("nvim", cfg["relaunch_programs"])

    def test_the_default_list_is_not_shared_with_the_module(self):
        cfg = self.load()
        cfg["relaunch_programs"].append("psql")
        self.assertNotIn("psql", config_mod.DEFAULTS["relaunch_programs"])


class TestRelaunchKeys(ConfigTestCase):
    def test_a_custom_allowlist_replaces_the_default(self):
        self.write({"relaunch_programs": ["nvim", "tig"]})
        self.assertEqual(self.load()["relaunch_programs"], ["nvim", "tig"])

    def test_an_empty_allowlist_is_honoured_not_treated_as_missing(self):
        self.write({"relaunch_programs": []})
        self.assertEqual(self.load()["relaunch_programs"], [])

    def test_a_non_list_allowlist_falls_back_to_the_default(self):
        self.write({"relaunch_programs": "nvim"})
        self.assertIn("nvim", self.load()["relaunch_programs"])
        self.assertIn("htop", self.load()["relaunch_programs"])

    def test_a_list_with_a_non_string_falls_back_to_the_default(self):
        self.write({"relaunch_programs": ["nvim", 7]})
        self.assertEqual(
            self.load()["relaunch_programs"], config_mod.DEFAULTS["relaunch_programs"]
        )

    def test_the_boolean_switches_are_read(self):
        self.write({"restore_process": False, "relaunch_agents": False})
        cfg = self.load()
        self.assertFalse(cfg["restore_process"])
        self.assertFalse(cfg["relaunch_agents"])

    def test_a_non_boolean_switch_falls_back(self):
        self.write({"restore_process": "no"})
        self.assertTrue(self.load()["restore_process"])


class TestCorruptFile(ConfigTestCase):
    def test_invalid_json_yields_defaults(self):
        self.write("{not json::")
        self.assertTrue(self.load()["restore_process"])

    def test_a_json_array_yields_defaults(self):
        self.write("[1, 2, 3]")
        self.assertTrue(self.load()["restore_process"])

    def test_unknown_keys_are_ignored(self):
        self.write({"nonsense": 1, "restore_process": False})
        cfg = self.load()
        self.assertNotIn("nonsense", cfg)
        self.assertFalse(cfg["restore_process"])


if __name__ == "__main__":
    unittest.main()
