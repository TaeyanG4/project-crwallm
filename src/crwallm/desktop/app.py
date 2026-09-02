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

from crwallm.desktop.bridge import Bridge

__all__ = ["run", "ui_root"]

TITLE = "모으기"
WIDTH = 1000
HEIGHT = 760
MIN_SIZE = (720, 560)


def ui_root() -> Path:
    """Where index.html lives, whether run from source or from a build.

    PyInstaller unpacks data files into ``sys._MEIPASS`` and leaves
    ``__file__`` pointing inside the frozen archive, so the ordinary answer is
    wrong in exactly the build that matters.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "crwallm" / "desktop" / "ui"
    return Path(__file__).resolve().parent / "ui"


def run(*, debug: bool = False, allow_local: bool = False) -> int:
    """Open the window and block until it is closed."""
    try:
        import webview
    except ImportError:
        print(
            "데스크톱 화면을 열려면 pywebview가 필요합니다.\n  uv sync",
            file=sys.stderr,
        )
        return 1

    index = ui_root() / "index.html"
    if not index.exists():
        print(f"화면 파일을 찾지 못했습니다: {index}", file=sys.stderr)
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
        print(_startup_error(exc), file=sys.stderr)
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
