"""Tests for herdr_undo_close.api: HerdrApi.call() opens a fresh AF_UNIX
connection per call and always closes it, even on an error response. A
fake socket class stands in for socket.socket so no real socket is used."""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from herdr_undo_close.api import HerdrApi, HerdrApiError


class FakeSocket(object):
    """Answers exactly one newline-terminated JSON response, echoing the
    request's id, and tracks connect/send/close for assertions."""

    def __init__(self, error=None):
        self.error = error
        self.connected_to = None
        self.sent = []
        self.closed = False
        self._pending = b""

    def settimeout(self, timeout):
        pass

    def connect(self, path):
        self.connected_to = path

    def sendall(self, data):
        self.sent.append(data)
        payload = json.loads(data.decode("utf-8").strip())
        if self.error is not None:
            response = {"id": payload["id"], "error": self.error}
        else:
            response = {"id": payload["id"], "result": {"echo": payload["method"]}}
        self._pending = (json.dumps(response) + "\n").encode("utf-8")

    def recv(self, nbytes):
        data, self._pending = self._pending, b""
        return data

    def close(self):
        self.closed = True


class TestHerdrApiCallPerConnection(unittest.TestCase):
    def _patch_socket(self, error=None):
        created = []

        def factory(family, kind):
            sock = FakeSocket(error=error)
            created.append(sock)
            return sock

        patcher = mock.patch("socket.socket", side_effect=factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        return created

    def test_each_call_opens_a_new_socket_and_closes_it(self):
        created = self._patch_socket()
        api = HerdrApi(socket_path="/fake/herdr.sock")

        result1 = api.call("workspace.list")
        result2 = api.call("tab.list")

        self.assertEqual(len(created), 2)
        self.assertIsNot(created[0], created[1])
        for sock in created:
            self.assertTrue(sock.closed)
            self.assertEqual(sock.connected_to, "/fake/herdr.sock")
        self.assertEqual(result1, {"echo": "workspace.list"})
        self.assertEqual(result2, {"echo": "tab.list"})

    def test_error_response_raises_and_still_closes_socket(self):
        created = self._patch_socket(error="boom")
        api = HerdrApi(socket_path="/fake/herdr.sock")

        with self.assertRaises(HerdrApiError):
            api.call("tab.list")

        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].closed)


if __name__ == "__main__":
    unittest.main()
