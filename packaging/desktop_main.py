"""What the built executable runs.

A file of its own rather than pointing PyInstaller at the module: a frozen app
re-executes its own entry point for every child process it starts, so the
top-level of this file has to be safe to run more than once, and
``freeze_support`` has to come before anything else does.
"""

from __future__ import annotations

import multiprocessing
import sys


def _self_test(*, quiet: bool) -> int:
    """``CRWALLM.exe --self-test``: prove the build before trusting it.

    Written to a file as well as shown, because the interesting case is a
    machine that is not this one and a person who can only send a file back.
    ``--quiet`` skips the message box - the box is modal, and a build script
    waiting for someone to click OK is a build script that never finishes.
    """
    import tempfile
    from pathlib import Path

    import crwallm.desktop.app as app
    from crwallm.desktop.selftest import run_self_test

    code, report = run_self_test()
    log = Path(tempfile.gettempdir()) / "crwallm-self-test.txt"
    log.write_text(report, encoding="utf-8")

    if not quiet:
        app._FROM_ICON = True  # there is no console here; put it on screen
        app._report(f"{report}\n\n기록: {log}")
    return code


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if "--self-test" in sys.argv:
        sys.exit(_self_test(quiet="--quiet" in sys.argv))

    from crwallm.desktop.app import main

    sys.exit(main())
