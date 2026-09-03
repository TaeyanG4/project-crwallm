"""The window.

Everything the desktop app is: one window, one HTML file, one bridge object.
No server, no port, no browser, no Node - the page is loaded from disk and
talks to Python directly through ``window.pywebview.api``.

That is the whole reason this path exists. The web UI needs Postgres, a
migration, an API process, a worker and a Next.js dev server before anything
appears on screen, and every one of those is a place for someone who did not
ask to be a system administrator to get stuck. This needs a file and a window.
"""

from __future__ import annotations

import sys
from pathlib import Path

from crwallm import ui
from crwallm.desktop.bridge import Bridge

__all__ = ["main", "run", "ui_root"]

TITLE = "모으기"
WIDTH = 1000
HEIGHT = 760
MIN_SIZE = (720, 560)


def ui_root() -> Path:
    """Where index.html lives, whether run from source or from a build.

    Kept as a name here because it reads better at the call site below, but
    the page is no longer the desktop's own - the browser serves the same
    files, so they live in ``crwallm.ui`` and both hosts ask it.
    """
    return ui.root()


_FROM_ICON = False
"""Whether this process was started by double-clicking rather than by typing.

Set by ``main`` and read by ``_report``, and it is the only reliable way to
know. Sniffing the environment was tried and does not work: ``sys.stderr`` is
not None when a parent redirects it, and ``GetConsoleWindow`` still answers yes
for a pythonw process launched from a shell, so both say "someone is reading
this" in exactly the case where nobody is. The caller knows; nothing else
does."""


def main() -> int:
    """The entry point the icon starts.

    Registered under ``gui-scripts`` rather than ``scripts``. That is the whole
    difference between double-clicking and getting an app, and double-clicking
    and getting a black console window with the app behind it - one the person
    is then invited to close, taking the app with it.
    """
    global _FROM_ICON
    _FROM_ICON = True
    return run()


def _report(message: str) -> None:
    """Say what went wrong somewhere the person will actually see it.

    Started from the icon there is nowhere to print. Someone who double-clicks,
    gets no window and is given no reason has no move left, so the last resort
    is the one thing Windows will always put on screen. Run from a terminal it
    stays a printed line, because a modal box in the middle of a shell session
    is its own kind of rude.
    """
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if _FROM_ICON and sys.platform == "win32":
        import ctypes

        # MB_ICONERROR, and modal on purpose: the process exits as soon as this
        # returns, and an unread message would exit with it.
        ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x00000010)


def run(*, debug: bool = False, allow_local: bool = False) -> int:
    """Open the window and block until it is closed."""
    try:
        import webview
    except ImportError:
        _report("화면을 열려면 pywebview가 필요합니다.\n\n  uv sync")
        return 1

    index = ui_root() / "index.html"
    if not index.exists():
        _report(f"화면 파일을 찾지 못했습니다:\n{index}")
        return 1

    bridge = Bridge(allow_local=allow_local)

    window = webview.create_window(
        TITLE,
        url=index.as_uri(),
        js_api=bridge,
        width=WIDTH,
        height=HEIGHT,
        min_size=MIN_SIZE,
        text_select=True,
        background_color="#f4f4f5",
    )

    try:
        # private_mode leaves nothing behind between runs, which is right for a
        # tool that holds no account and remembers no site.
        webview.start(bridge._attach, (window,), debug=debug, private_mode=True)
    except Exception as exc:  # pragma: no cover - depends on the machine
        _report(_startup_error(exc))
        return 1
    finally:
        bridge._shutdown()
    return 0


def _startup_error(exc: Exception) -> str:
    """The one failure that is not the user's fault and not ours.

    Windows 10 does not always ship WebView2. The message it produces is a
    class name; the thing to do about it is a download.
    """
    text = str(exc)
    if "WebView2" in text or "Edge" in text or "webview2" in text.lower():
        return (
            "화면을 그리는 데 필요한 구성 요소(WebView2)가 없습니다.\n"
            "아래 주소에서 'Evergreen Bootstrapper'를 내려받아 설치한 뒤 "
            "다시 실행해주세요.\n"
            "  https://developer.microsoft.com/microsoft-edge/webview2/"
        )
    return f"창을 열지 못했습니다: {type(exc).__name__}: {text}"
