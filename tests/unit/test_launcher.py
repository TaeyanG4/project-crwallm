"""One door, and it does not need Docker to open.

The icon used to start the whole stack - Docker check, database, migration,
API, worker, Next.js - before anything appeared. That is five prerequisites in
front of a program whose main job is to read one page, and every one of them
is a place to get stuck with nothing on screen explaining it.

The launcher now starts the window and nothing else. These are the checks that
keep that true, plus the two Windows-specific traps that made earlier versions
fail in ways nothing in Python could see.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

import crwallm.desktop.app as desktop_app

ROOT = Path(__file__).resolve().parents[2]
BAT = (ROOT / "crwallm.bat").read_bytes()


def commands() -> str:
    """The lines cmd will actually run.

    A .bat is half explanation. Reading the comments as instructions would make
    the sentence "No Docker, no database" look exactly like starting one.
    """
    lines = BAT.decode("ascii", errors="replace").splitlines()
    return "\n".join(
        line for line in lines if not line.strip().lower().startswith(("rem", "::", "echo"))
    ).lower()


@pytest.fixture(autouse=True)
def icon_flag_is_restored() -> Iterator[None]:
    """``main`` sets a module global. Leaving it set would leak into the rest of
    the suite, where a modal message box is the last thing anyone wants."""
    before = desktop_app._FROM_ICON
    try:
        yield
    finally:
        desktop_app._FROM_ICON = before


def test_the_launcher_is_ascii() -> None:
    """cmd parses a .bat in the system codepage before running a line of it.

    UTF-8 Korean in this file is read as byte soup and its fragments are run as
    commands - the machine this was written on printed
    "'es'는 내부 또는 외부 명령이 아닙니다" and stopped. ``chcp 65001`` on line
    two does not help, because the damage is done at parse time. Everything a
    person is meant to read comes from Python instead, which controls its own
    encoding.
    """
    try:
        BAT.decode("ascii")
    except UnicodeDecodeError as exc:
        line = BAT[: exc.start].count(b"\n") + 1
        pytest.fail(
            f"crwallm.bat is not ASCII (byte {exc.start}, line {line}). "
            "cmd will read it in the system codepage and execute the pieces."
        )


def test_the_icon_opens_the_window_and_nothing_else() -> None:
    """No Docker, no database, no API, no worker, no web server.

    Docker is only wanted by the parts that keep a history, and none of those
    are on this path.
    """
    ran = commands()
    assert "crwallm-desktop.exe" in ran

    for absent in ("docker", "compose", "alembic", "npm", "crwallm up", "--launcher"):
        assert absent not in ran, f"the launcher still reaches for {absent!r}"


def test_the_icon_starts_the_console_less_entry_point() -> None:
    """A ``scripts`` entry point leaves a black cmd window behind the app.

    Someone closes it, because it looks like litter, and the app goes with it.
    ``gui-scripts`` builds the launcher against pythonw instead, and the .bat
    hands off to it and exits.
    """
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["project"].get("gui-scripts", {}).get("crwallm-desktop") == (
        "crwallm.desktop.app:main"
    )
    assert callable(desktop_app.main)


def test_the_icon_path_knows_it_is_the_icon_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed launch reaches a person only if the code knows nobody is
    watching stdout - and from an icon, nobody is.

    Environment sniffing was tried first and is wrong in both directions:
    ``sys.stderr`` is a real stream whenever a parent redirects it, and
    ``GetConsoleWindow`` still answers yes for a pythonw process started from a
    shell. Both say "someone is reading this" in the one case where nobody is,
    so the caller declares it instead - which means this flag has to get set.
    """
    said: list[str] = []
    monkeypatch.setattr(desktop_app, "ui_root", lambda: Path("Z:/nowhere"))
    # Patched, not exercised: the real one puts up a modal box, and a test suite
    # that stops for a dialog never finishes. That path is verified by hand.
    monkeypatch.setattr(desktop_app, "_report", said.append)

    assert desktop_app.main() == 1
    assert desktop_app._FROM_ICON is True
    assert said, "a launch that opened no window said nothing about why"


SPEC = (ROOT / "packaging" / "crwallm.spec").read_text(encoding="utf-8")


def test_the_build_puts_the_screen_where_the_app_looks_for_it() -> None:
    """Two halves of one path, written in two files.

    ``ui_root`` builds ``<bundle>/crwallm/ui`` and the spec decides
    where PyInstaller unpacks it. Disagree and the packaged app opens a window
    onto nothing - no error, no console, a blank white rectangle.
    """
    frozen = "C:/fake-bundle"
    try:
        desktop_app.sys._MEIPASS = frozen  # type: ignore[attr-defined]
        inside = desktop_app.ui_root()
    finally:
        del desktop_app.sys._MEIPASS  # type: ignore[attr-defined]

    relative = inside.relative_to(Path(frozen)).as_posix()
    assert relative == "crwallm/ui"
    assert f'"{relative}"' in SPEC, (
        f"ui_root() reads {relative} out of the bundle; the spec must put it there"
    )


def test_the_build_has_no_console() -> None:
    """Same reason the .bat hands off to a gui-scripts launcher."""
    assert "console=False" in SPEC.replace(" ", "")


def test_the_installer_carries_a_byte_order_mark() -> None:
    """Windows PowerShell 5.1 reads a .ps1 without a BOM in the system
    codepage, which turns every Korean line in the installer into mojibake -
    the same trap that made the .bat ASCII-only, in the one file whose whole
    job is to talk to the person installing."""
    raw = (ROOT / "packaging" / "install.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), (
        "packaging/install.ps1 must be UTF-8 with a BOM, or PowerShell 5.1 "
        "will read its Korean as the system codepage"
    )


def test_the_cli_can_be_run_without_activating_anything() -> None:
    """`crwallm serve` in a fresh shell is `command not found`, every time.

    The command lives in .venv, which is not on PATH until it is activated -
    correct, and also the first thing everybody trips over. ./crwallm forwards
    to `uv run crwallm`, which needs no preparation at all.
    """
    shim = ROOT / "crwallm"

    assert shim.exists(), "./crwallm is what the docs tell people to type"
    text = shim.read_text(encoding="utf-8")
    assert text.startswith("#!"), "no shebang means bash will not run it"
    assert "uv run crwallm" in text
    # No `cd`: uv walks up to find the project, and moving would re-root every
    # relative path the caller passed, so `-o out.csv` would land elsewhere.
    assert "\ncd " not in text


def test_the_terminal_path_stays_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """``crwallm desktop`` in a shell must not raise a dialog: there is a
    console right there, and a modal box in the middle of a shell session is
    its own kind of rude."""
    monkeypatch.setattr(desktop_app, "ui_root", lambda: Path("Z:/nowhere"))
    monkeypatch.setattr(desktop_app, "_report", lambda message: None)

    assert desktop_app.run() == 1
    assert desktop_app._FROM_ICON is False
