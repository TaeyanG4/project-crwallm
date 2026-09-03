"""Start everything, in one command.

The tool needs three processes: an API, a worker that runs queued crawls, and
the web UI. Asking someone to open three terminals in the right order, and to
remember that the UI is useless without the worker, is a setup step disguised
as a design.

**Subprocesses, not threads.** The UI is Node and the worker wants its own
event loop and its own database sessions. One supervisor that starts three
children and takes them all down together is both simpler and more honest than
pretending they belong in one process.

**Output is prefixed and interleaved.** Three logs in one terminal is only
useful if you can tell which is which, and the alternative - hiding them - is
how a crawl that is failing looks like a crawl that is quiet.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import IO

import typer

__all__ = ["up"]

WEB_DIR = Path("web")
READY_TIMEOUT_S = 90.0

_COLOURS = {
    "api": typer.colors.CYAN,
    "worker": typer.colors.MAGENTA,
    "ui": typer.colors.GREEN,
}


def _say(prefix: str, message: str) -> None:
    typer.secho(f"{prefix:>7} ", fg=_COLOURS.get(prefix), nl=False)
    typer.echo(message)


def _pump(prefix: str, stream: IO[str] | None, quiet_markers: tuple[str, ...] = ()) -> None:
    """Forward one child's output, tagged.

    Runs on a thread because the alternative is a select loop over pipes that
    behaves differently on Windows, and there are three of them.
    """
    if stream is None:
        return
    for line in stream:
        text = line.rstrip()
        if not text or any(marker in text for marker in quiet_markers):
            continue
        _say(prefix, text)


class Supervisor:
    """Three children, one lifetime.

    Every exit path goes through ``stop``: a supervisor that leaves a Chromium
    or a Node process behind on Ctrl-C is worse than no supervisor, because the
    port stays busy and the next run fails for a reason that looks unrelated.
    """

    def __init__(self) -> None:
        self._children: list[tuple[str, subprocess.Popen[str]]] = []
        self._stopping = False
        self._job = _make_kill_on_close_job()

    def _adopt(self, child: subprocess.Popen[str]) -> None:
        """Tie a child's lifetime to this process, on Windows.

        Signals are only half the problem. A parent killed outright - Task
        Manager, a `taskkill /F`, an IDE stopping the run - never reaches its
        `finally`, and then a Node server and a uvicorn keep the ports busy
        while nothing owns them. Measured: ten orphaned processes and both
        ports still answering.

        A job object with ``KILL_ON_JOB_CLOSE`` makes the kernel do it. When
        the last handle closes - which happens when this process dies, however
        it dies - every process in the job goes with it.
        """
        if self._job is None:
            return
        with contextlib.suppress(Exception):
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(
                0x001F0FFF,  # PROCESS_ALL_ACCESS
                False,
                child.pid,
            )
            if handle:
                ctypes.windll.kernel32.AssignProcessToJobObject(self._job, handle)
                ctypes.windll.kernel32.CloseHandle(handle)

    def start(self, name: str, argv: list[str], *, cwd: Path | None = None) -> None:
        # Deliberately *not* CREATE_NEW_PROCESS_GROUP. That was the first
        # attempt, on the reasoning that the supervisor should control
        # shutdown rather than race the console for it - and it meant Ctrl-C
        # never reached the children at all. Measured: both ports still
        # answering after the parent was gone. uvicorn and Next both handle
        # Ctrl-C correctly on their own; letting them have it is the reliable
        # path, and `stop` below is for the stragglers.
        child = subprocess.Popen(  # noqa: S603 - argv is built here, not from input
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "FORCE_COLOR": "0"},
        )
        self._adopt(child)
        self._children.append((name, child))
        threading.Thread(
            target=_pump,
            args=(name, child.stdout),
            daemon=True,
        ).start()

    def poll(self) -> tuple[str, int] | None:
        """The first child that has exited, if any."""
        for name, child in self._children:
            code = child.poll()
            if code is not None:
                return name, code
        return None

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True

        for name, child in reversed(self._children):
            if child.poll() is not None:
                continue
            _say(name, "stopping")
            with contextlib.suppress(Exception):
                child.terminate()

        deadline = time.monotonic() + 10
        for _name, child in self._children:
            remaining = max(0.5, deadline - time.monotonic())
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=remaining)

        # Anything still alive after ten seconds is not going to stop politely.
        for name, child in self._children:
            if child.poll() is None:
                _say(name, "did not stop; killing")
                with contextlib.suppress(Exception):
                    child.kill()


def _make_kill_on_close_job() -> int | None:
    """A Windows job object whose children die when it is closed.

    Returns None everywhere else: on POSIX the children share this process's
    group and a terminal's Ctrl-C reaches them directly, so there is nothing
    to arrange.
    """
    if sys.platform != "win32":
        return None

    with contextlib.suppress(Exception):
        import ctypes
        from ctypes import wintypes

        class _LimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _LimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = ctypes.windll.kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        limits = _ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        ctypes.windll.kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        )
        return int(job)
    return None


def _api_is_up(host: str, port: int) -> bool:
    import httpx

    with contextlib.suppress(Exception):
        return httpx.get(f"http://{host}:{port}/health", timeout=1.0).status_code == 200
    return False


def _port_is_busy(host: str, port: int) -> bool:
    import socket

    with socket.socket() as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((host, port)) == 0


def _ensure_web_deps(web: Path, npm: str) -> bool:
    if (web / "node_modules").exists():
        return True

    _say("ui", "installing dependencies (first run, this takes a minute)")
    result = subprocess.run(  # noqa: S603 - npm path resolved above
        [npm, "install", "--no-fund", "--no-audit"],
        cwd=web,
        check=False,
    )
    if result.returncode != 0:
        _say("ui", "npm install failed")
        return False
    return True


def _prepare(settings: object) -> str | None:
    """Bring up what the stack needs, or say what is missing.

    Lives here rather than in the launcher script because a ``.bat`` is parsed
    in the system codepage and cannot carry a Korean sentence at all - and
    because this is the sort of thing that wants testing, which a batch file
    resists.

    Returns a message to show and stop on, or None to continue.
    """
    from pathlib import Path as _Path

    docker = shutil.which("docker")
    if docker is None:
        return (
            "Docker가 없습니다.\n"
            "        Docker Desktop을 설치하고 켜주세요 - 데이터베이스가 그 위에서 돕니다."
        )

    probe = subprocess.run(  # noqa: S603 - docker path resolved above
        [docker, "info"], capture_output=True, check=False
    )
    if probe.returncode != 0:
        return "Docker가 실행 중이 아닙니다.\n        Docker Desktop을 켜고 다시 시도해주세요."

    if not _Path(".env").exists():
        _say("setup", "설정 파일이 없습니다. `crwallm setup`을 먼저 실행해주세요.")
        return (
            "설정이 끝나지 않았습니다.\n"
            "        crwallm setup  을 실행하면 설정 파일·DB·모델을 준비합니다."
        )

    _say("db", "starting")
    started = subprocess.run(  # noqa: S603 - docker path resolved above
        [docker, "compose", "up", "-d", "db"], capture_output=True, check=False
    )
    if started.returncode != 0:
        detail = started.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        return "데이터베이스를 시작하지 못했습니다.\n        " + detail[0][:160]

    # Cheap when already current, and the alternative is a first crawl that
    # fails on a missing column.
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        check=False,
    )
    return None


def up(
    host: str = "127.0.0.1",
    port: int = 8000,
    ui_port: int = 3000,
    *,
    with_web: bool = False,
    with_worker: bool = True,
    open_browser: bool = True,
    launcher: bool = False,
) -> int:
    """Run the API, the worker and the UI until interrupted.

    The UI is the API's own page now, on the API's own port, so "the UI" costs
    no extra process and no Node. ``with_web`` starts the older Next.js app
    beside it, which is the only thing here that needs either.
    """
    from crwallm.config import get_settings

    settings = get_settings()

    if launcher:
        # Double-clicked rather than typed: nothing has been prepared, and the
        # person reading this did not choose to be at a terminal.
        problem = _prepare(settings)
        if problem is not None:
            typer.echo("")
            typer.secho(f"  {problem}", fg=typer.colors.RED)
            typer.echo("")
            return 1
    host = host or settings.api_host
    port = port or settings.api_port

    if _port_is_busy(host, port):
        typer.secho(
            f"something is already listening on {host}:{port} - stop it, or pass --port",
            fg=typer.colors.RED,
            err=True,
        )
        return 1

    web = Path.cwd() / WEB_DIR
    npm = shutil.which("npm")

    if with_web:
        if not web.exists():
            _say("web", f"no {WEB_DIR}/ here; skipping it")
            with_web = False
        elif npm is None:
            _say("web", "npm is not on PATH; skipping it (everything else still runs)")
            with_web = False
        elif not _ensure_web_deps(web, npm):
            with_web = False

    supervisor = Supervisor()
    stop_requested = threading.Event()

    def handle_signal(_signum: int, _frame: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGINT, handle_signal)
    with contextlib.suppress(AttributeError, ValueError):
        signal.signal(signal.SIGTERM, handle_signal)

    try:
        supervisor.start(
            "api",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "crwallm.api.app:app",
                "--host",
                host,
                "--port",
                str(port),
            ],
        )

        # The worker is started only once the API answers, so its first log
        # line is not a database error from a stack that is still coming up.
        deadline = time.monotonic() + READY_TIMEOUT_S
        while not _api_is_up(host, port):
            if stop_requested.is_set():
                return 130
            exited = supervisor.poll()
            if exited is not None:
                _say(exited[0], f"exited with {exited[1]} before the API was ready")
                return 1
            if time.monotonic() > deadline:
                typer.secho("the API did not come up in time", fg=typer.colors.RED, err=True)
                return 1
            time.sleep(0.3)

        _say("api", f"http://{host}:{port}  (docs at /docs)")

        if with_worker:
            supervisor.start("worker", [sys.executable, "-m", "crwallm.cli.main", "worker"])

        # The window's page is served by the API itself, on the API's port.
        # That is the whole of the UI unless someone asks for the older
        # Next.js one, which is the only thing here that needs Node.
        _say("ui", f"http://{host}:{port}")

        if with_web and npm is not None:
            supervisor.start(
                "web",
                [npm, "run", "dev", "--", "--port", str(ui_port)],
                cwd=web,
                # Next prints a banner and a compile line per route; the
                # useful part is the URL, which the line below already gives.
            )
            _say("web", f"http://localhost:{ui_port}  (jobs, recipes, chat)")

        typer.echo("")
        typer.secho("  ready — Ctrl-C to stop everything", fg=typer.colors.GREEN, bold=True)
        typer.echo("")

        if open_browser:
            landing = f"http://localhost:{ui_port}/chat" if with_web else f"http://{host}:{port}/"
            _open_later(landing)

        while not stop_requested.is_set():
            exited = supervisor.poll()
            if exited is not None:
                name, code = exited
                _say(name, f"exited with {code}")
                break
            time.sleep(0.4)

        return 0
    finally:
        typer.echo("")
        supervisor.stop()


def _open_later(url: str, delay: float = 3.0) -> None:
    """Open the browser once the UI has had a moment to compile.

    A failure here is not a failure of the run: the URL is printed above and
    a machine with no browser is a legitimate place to run this.
    """

    def go() -> None:
        with contextlib.suppress(Exception):
            webbrowser.open(url)

    threading.Timer(delay, go).start()
