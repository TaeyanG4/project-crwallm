"""The page, and where to find it.

One HTML file, one stylesheet, one script, shared by both front ends. It used
to live under ``desktop/``, which was true right up until the browser started
serving the same page - at which point "the desktop app's UI" was a name that
made the API import from a front end to find it.

Nothing here is Python except this function. It exists because the answer
changes when the app is frozen: PyInstaller unpacks data files into
``sys._MEIPASS`` and leaves ``__file__`` pointing inside the archive, so the
ordinary answer is wrong in exactly the build that matters.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["INDEX", "root"]

INDEX = "index.html"


def root() -> Path:
    """The directory holding index.html, from source or from a build."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "crwallm" / "ui"
    return Path(__file__).resolve().parent
