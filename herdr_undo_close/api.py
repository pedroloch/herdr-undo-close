"""Minimal client for the Herdr plugin socket protocol.

Connects AF_UNIX to HERDR_SOCKET_PATH. The Herdr control socket accepts
exactly ONE request per connection: once it has written back the response
it closes the connection, so reusing the same socket for a second request
raises "[Errno 32] Broken pipe" on the next sendall (verified empirically
against Herdr 0.7.5). Every call() therefore opens its own fresh
connection, sends one newline-terminated JSON object
({"id","method","params"}), reads one newline-terminated JSON response,
and closes the socket - no connection is reused across calls, and none is
held open for the duration of a hook invocation.
"""

import json
import os
import socket
from typing import Any, Dict, Optional


class HerdrApiError(Exception):
    """Raised when the socket cannot be reached or a request errors out."""


class HerdrApi(object):
    """Client for the Herdr control socket: a fresh AF_UNIX connection per call()."""

    def __init__(self, socket_path=None, timeout=5.0):
        # type: (Optional[str], float) -> None
        self.socket_path = socket_path or os.environ.get("HERDR_SOCKET_PATH")
        if not self.socket_path:
            raise HerdrApiError("HERDR_SOCKET_PATH is not set")
        self._timeout = timeout
        self._next_id = 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def _read_line(self, sock):
        # type: (socket.socket) -> bytes
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise HerdrApiError("socket closed before a full response arrived")
            buf += chunk
        line, _, _ = buf.partition(b"\n")
        return line

    def call(self, method, params=None):
        # type: (str, Optional[Dict[str, Any]]) -> Dict[str, Any]
        """Open a fresh connection, send one request, read one response,
        and close the connection - always, even on error."""
        req_id = str(self._next_id)
        self._next_id += 1
        payload = {"id": req_id, "method": method, "params": params or {}}
        data = (json.dumps(payload) + "\n").encode("utf-8")

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        try:
            try:
                sock.connect(self.socket_path)
                sock.sendall(data)
                line = self._read_line(sock)
            except OSError as exc:
                raise HerdrApiError("request %s failed: %s" % (method, exc))
        finally:
            sock.close()

        try:
            response = json.loads(line.decode("utf-8"))
        except ValueError as exc:
            raise HerdrApiError("invalid JSON response for %s: %s" % (method, exc))
        if not isinstance(response, dict):
            raise HerdrApiError("unexpected response shape for %s" % method)
        if response.get("id") != req_id:
            raise HerdrApiError(
                "response id mismatch for %s: expected %s, got %s"
                % (method, req_id, response.get("id"))
            )
        if "error" in response:
            raise HerdrApiError("%s error: %s" % (method, response["error"]))
        result = response.get("result", {})
        return result if isinstance(result, dict) else {}
