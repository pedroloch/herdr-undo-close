"""Tests for herdr_undo_close.store: atomic round-trip, corrupt recovery,
locking, and trimming. No sockets, no network."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from herdr_undo_close import store


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix="herdr-undo-close-test-")

    def tearDown(self):
        shutil.rmtree(self.state_dir, ignore_errors=True)


class TestAtomicRoundTrip(StoreTestCase):
    def test_save_then_load_round_trips(self):
        state = {
            "version": 1,
            "index": {"w1:t1": {"tab_id": "w1:t1", "refreshed_ms": 100}},
            "closed": [{"tab_id": "w1:t2", "closed_ms": 50}],
        }
        store.save_state(self.state_dir, state)
        loaded = store.load_state(self.state_dir)
        self.assertEqual(loaded, state)

    def test_save_replaces_previous_contents(self):
        store.save_state(self.state_dir, store.default_state())
        second = {"version": 1, "index": {"a": {"refreshed_ms": 1}}, "closed": []}
        store.save_state(self.state_dir, second)
        self.assertEqual(store.load_state(self.state_dir), second)

    def test_save_leaves_no_temp_files_behind(self):
        store.save_state(self.state_dir, store.default_state())
        leftovers = [n for n in os.listdir(self.state_dir) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class TestCorruptRecovery(StoreTestCase):
    def test_missing_file_yields_default_state(self):
        loaded = store.load_state(self.state_dir)
        self.assertEqual(loaded, store.default_state())

    def test_corrupt_json_yields_empty_state(self):
        path = os.path.join(self.state_dir, store.STATE_FILENAME)
        with open(path, "w") as f:
            f.write("{not valid json::")
        loaded = store.load_state(self.state_dir)
        self.assertEqual(loaded, store.default_state())

    def test_wrong_shape_yields_empty_state(self):
        path = os.path.join(self.state_dir, store.STATE_FILENAME)
        with open(path, "w") as f:
            f.write('{"version": 1, "index": [], "closed": {}}')
        loaded = store.load_state(self.state_dir)
        self.assertEqual(loaded, store.default_state())

    def test_non_dict_json_yields_empty_state(self):
        path = os.path.join(self.state_dir, store.STATE_FILENAME)
        with open(path, "w") as f:
            f.write("[1, 2, 3]")
        loaded = store.load_state(self.state_dir)
        self.assertEqual(loaded, store.default_state())


class TestLocking(StoreTestCase):
    def test_locked_context_manager_can_be_entered_and_reentered(self):
        with store.locked(self.state_dir):
            state = store.load_state(self.state_dir)
            state["index"]["x"] = {"refreshed_ms": 1}
            store.save_state(self.state_dir, state)

        with store.locked(self.state_dir):
            state = store.load_state(self.state_dir)
            self.assertIn("x", state["index"])

    def test_lock_file_is_created(self):
        with store.locked(self.state_dir):
            pass
        self.assertTrue(os.path.exists(os.path.join(self.state_dir, store.LOCK_FILENAME)))


class TestTrim(StoreTestCase):
    def test_closed_trimmed_to_max_entries(self):
        state = {
            "version": 1,
            "index": {},
            "closed": [{"tab_id": "t%d" % i, "closed_ms": i} for i in range(5)],
        }
        store.trim(state, max_entries=2)
        self.assertEqual(len(state["closed"]), 2)
        self.assertEqual([c["tab_id"] for c in state["closed"]], ["t0", "t1"])

    def test_closed_left_alone_when_under_the_cap(self):
        state = {
            "version": 1,
            "index": {},
            "closed": [{"tab_id": "t0", "closed_ms": 0}],
        }
        store.trim(state, max_entries=25)
        self.assertEqual(len(state["closed"]), 1)

    def test_index_trimmed_to_most_recently_refreshed(self):
        state = {
            "version": 1,
            "index": {
                "old": {"refreshed_ms": 1},
                "mid": {"refreshed_ms": 5},
                "new": {"refreshed_ms": 10},
            },
            "closed": [],
        }
        store.trim(state, max_entries=25, max_index=2)
        self.assertEqual(set(state["index"].keys()), {"mid", "new"})


if __name__ == "__main__":
    unittest.main()
