"""The README tells people what to type. It should be typeable.

Documentation rots differently from code: nothing fails, the reader just runs
a command that does not exist and concludes the tool is broken. This project
has already shipped an install section that did not work
(``uv venv`` where ``uv sync`` was needed) and a launcher whose documented
command had been renamed underneath it.

So every command, flag, file and link the README names is checked against the
repository that ships with it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")

ENV = {
    **os.environ,
    "PYTHONIOENCODING": "utf-8",
    # Wide, on purpose. Typer renders its help through rich, which fits the
    # output to the terminal and *truncates* what does not fit - at 40 columns
    # `--no-browser` is drawn as `--no-br…`, and a substring check on that says
    # the flag does not exist. CI has no TTY and picked a width narrow enough
    # to do it, so this passed on a developer's terminal and failed there.
    "COLUMNS": "200",
}


def cli(*args: str) -> tuple[int, str]:
    """The CLI's own help, and whether it recognised the command.

    Decoded here rather than by the console: ``text=True`` would use the
    console codepage - cp949 on the machine this was written on - and typer
    draws its help with box-drawing characters, which is a UnicodeDecodeError
    rather than a test result.
    """
    result = subprocess.run(
        [sys.executable, "-m", "crwallm.cli.main", *args], capture_output=True, env=ENV
    )
    return result.returncode, result.stdout.decode("utf-8", errors="replace")


def named_commands() -> set[tuple[str, ...]]:
    """Every ``crwallm ...`` the README tells someone to run."""
    found = set()
    for line in re.findall(r"`crwallm ([^`]+)`", README):
        words = [w for w in line.split() if not w.startswith(("-", "<", "["))]
        if words:
            found.add(tuple(words[:2]))
    return found


def test_every_command_the_readme_names_exists() -> None:
    """Including subcommands: ``recipe adapt`` is two words, and checking only
    the first would pass on a group whose member had been renamed.

    Asked by exit code rather than by matching the usage line. The first
    version looked for "Usage: crwallm ..." and the program calls itself
    "python -m crwallm.cli.main", so every command in the README came back
    missing - a check that fails on everything is a check nobody reads.
    """
    missing = [
        " ".join(command) for command in sorted(named_commands()) if cli(*command, "--help")[0] != 0
    ]
    assert not missing, f"README names commands that do not exist: {missing}"


def test_every_flag_the_readme_names_exists() -> None:
    """A flag on the wrong command is the same mistake as a missing one.

    ``--url`` belongs to ``recipe adapt``, not to the ``recipe`` group, and an
    earlier version of this check reported it missing because it looked at the
    group - a false alarm teaches people to ignore the check.
    """
    wrong = []
    for line in re.findall(r"`crwallm ([^`]+)`", README):
        words = line.split()
        command = [w for w in words if not w.startswith(("-", "<", "["))][:2]
        flags = [w for w in words if w.startswith("--")]
        if not command or not flags:
            continue
        code, help_text = cli(*command, "--help")
        for flag in flags:
            if flag not in help_text:
                # Carry what was seen, not just the name. A bare list cannot
                # tell "the flag was renamed" from "the help rendered
                # differently on this machine", and guessing between those from
                # a developer's terminal has already cost two CI runs.
                wrong.append(
                    f"{' '.join(command)} {flag}  (exit={code}, COLUMNS={ENV.get('COLUMNS')})\n"
                    + textwrap.indent(help_text.strip() or "<no output>", "      ")
                )
    assert not wrong, "README names flags their command does not have:\n" + "\n".join(wrong)


@pytest.mark.parametrize(
    "path", ["crwallm.bat", "crwallm", "packaging/build.py", "packaging/install.ps1"]
)
def test_the_files_the_readme_tells_you_to_run_exist(path: str) -> None:
    # Either separator: a PowerShell block writes packaging\install.ps1 and a
    # bash one writes packaging/install.ps1, and both are the same file.
    mentioned = path in README or path.replace("/", "\\") in README
    assert mentioned, f"{path} is no longer mentioned; drop it from this list"
    assert (ROOT / path).exists(), f"README tells people to run {path}, which is not here"


def test_every_link_resolves() -> None:
    """A doc index of sixteen files is exactly where a rename goes unnoticed."""
    broken = []
    for label, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", README):
        if target.startswith(("http://", "https://", "#")):
            continue
        if not (ROOT / target.split("#")[0]).exists():
            broken.append(f"[{label}]({target})")
    assert not broken, f"broken links in README: {broken}"


def test_every_anchor_resolves() -> None:
    """In-page links break silently: the browser scrolls nowhere and the reader
    assumes they missed something."""
    headings = re.findall(r"^#{1,6}\s+(.+?)\s*$", README, re.MULTILINE)
    anchors = {re.sub(r"[^\w\s가-힣-]", "", h.lower()).strip().replace(" ", "-") for h in headings}

    broken = [
        f"[{label}]({target})"
        for label, target in re.findall(r"\[([^\]]+)\]\((#[^)]+)\)", README)
        if target[1:] not in anchors
    ]
    assert not broken, f"anchors that go nowhere: {broken}  (have: {sorted(anchors)})"
