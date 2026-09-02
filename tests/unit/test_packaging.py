"""Every third-party module imported is declared as a dependency.

Found by looking, not by failing: ``selectolax`` was imported by six modules
and named in none of them, so the package installed fine and died on the first
page it tried to parse. ``httpcore``, ``starlette`` and ``zstandard`` were
reached through other packages that happen to pull them, which works until an
upstream release stops doing that.

The environment this runs in already has everything installed, so no test that
imports the code can catch this. Only reading the declaration can.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

IMPORT_TO_DISTRIBUTION = {
    "yaml": "pyyaml",
    "pydantic_settings": "pydantic-settings",
    "webview": "pywebview",
}
"""Where the import name and the package name differ."""


def declared_dependencies() -> set[str]:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return {
        re.split(r"[<>=\[;\s]", entry)[0].strip().lower()
        for entry in config["project"]["dependencies"]
    }


def imported_top_level_modules() -> set[str]:
    """Top-level module names imported anywhere under ``src``.

    Relative imports are skipped by requiring ``level == 0``: ``from .foo``
    names nothing that could be installed.
    """
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


def test_every_import_is_declared() -> None:
    declared = declared_dependencies()
    third_party = {
        module
        for module in imported_top_level_modules()
        if module not in sys.stdlib_module_names and module != "crwallm"
    }

    undeclared = sorted(
        module
        for module in third_party
        if IMPORT_TO_DISTRIBUTION.get(module, module).lower() not in declared
    )
    assert not undeclared, (
        f"imported but not in pyproject dependencies: {undeclared}. "
        "A clean install will fail at the first use."
    )


def test_the_check_can_actually_fail() -> None:
    """A guard that cannot fail is not a guard.

    The audit rests on parsing both files correctly; if either came back empty
    the assertion above would pass vacuously and go on passing forever.
    """
    assert len(declared_dependencies()) > 5
    assert len(imported_top_level_modules()) > 10
