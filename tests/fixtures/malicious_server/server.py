"""Runs the malicious fixture on a real socket.

Real socket, not a TestClient shim: half of what these tests assert is about
streaming, timeouts and connection behaviour, none of which an ASGI transport
reproduces.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from dataclasses import dataclass

import uvicorn

from tests.fixtures.malicious_server.app import create_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass(frozen=True, slots=True)
class RunningServer:
    host: str
    port: int

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"


class MaliciousServer:
    """Uvicorn on a background thread.

    A thread rather than a task: the tests drive an async client on the main
    loop, and a server sharing that loop deadlocks the moment an endpoint
    blocks - which several of these do on purpose.
    """

    def __init__(self) -> None:
        self.port = _free_port()
        self.host = "127.0.0.1"
        config = uvicorn.Config(
            create_app(),
            host=self.host,
            port=self.port,
            log_level="error",
            access_log=False,
            # Some endpoints hold a connection open forever; do not let
            # shutdown wait on them.
            timeout_graceful_shutdown=1,
        )
        self._server = uvicorn.Server(config)
        self._thread: threading.Thread | None = None

    def start(self, timeout: float = 10.0) -> RunningServer:
        self._thread = threading.Thread(target=self._run, daemon=True, name="malicious-server")
        self._thread.start()
        self._wait_until_listening(timeout)
        return RunningServer(self.host, self.port)

    def _run(self) -> None:
        with contextlib.suppress(Exception):
            self._server.run()

    def _wait_until_listening(self, timeout: float) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if getattr(self._server, "started", False):
                return
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                if s.connect_ex((self.host, self.port)) == 0:
                    return
            time.sleep(0.02)
        raise RuntimeError(f"malicious server did not start on port {self.port}")

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
