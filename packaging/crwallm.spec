# PyInstaller build of the desktop app.
#
#   uv run pyinstaller packaging/crwallm.spec --noconfirm
#
# One folder, not one file. A --onefile build unpacks itself into a temp
# directory on every launch, which for a bundle this size is several seconds of
# nothing happening before the window appears - and "I double-clicked and
# nothing happened" is the exact impression this whole path exists to avoid.
# The installer hides the folder anyway.
#
# **No Playwright.** Its Python package is 110 MB of driver binaries, and even
# with all of it bundled the browser itself is a separate 150 MB download, so
# including it buys nothing. The built app fetches over HTTP only. Pages whose
# content is written by scripts come back empty rather than crashing - the
# crawler already turns a missing browser into a failed render and keeps the
# HTTP result, which was measured rather than assumed.
#
# The database half of the project is excluded for the same reason: the window
# never opens a connection, so Postgres, SQLAlchemy, Alembic, the API and the
# worker are all weight nobody on this path carries.

from pathlib import Path

HERE = Path(SPECPATH).resolve()  # noqa: F821 - PyInstaller injects this
ROOT = HERE.parent

datas = [
    # ui_root() looks here when frozen. If this mapping and that function
    # disagree the window opens on a blank page, so they are one edit apart.
    (str(ROOT / "src" / "crwallm" / "ui"), "crwallm/ui"),
    # tldextract falls back to a network fetch of the public suffix list when
    # its snapshot is missing - on a machine with no internet, or a site that
    # blocks it, every domain check would then fail at the worst moment.
    (
        str(ROOT / ".venv" / "Lib" / "site-packages" / "tldextract" / ".tld_set_snapshot"),
        "tldextract",
    ),
]

excludes = [
    # Rendering: see the note above.
    "playwright",
    # The database, the API and the worker. The window uses none of them.
    "sqlalchemy",
    "asyncpg",
    "alembic",
    "fastapi",
    "uvicorn",
    "starlette",
    "greenlet",
    # The CLI. This executable has one entry point and it is not a terminal.
    "typer",
    # Never wanted, and each one is tens of megabytes if something drags it in.
    "tkinter",
    "PIL",
    "numpy",
    "pytest",
    "_pytest",
    "mypy",
    "ruff",
    "IPython",
    "setuptools",
    "pip",
]

a = Analysis(  # noqa: F821
    [str(HERE / "desktop_main.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[
        # Reached through a registry rather than an import statement, so static
        # analysis cannot see them.
        "crwallm.crawler.extraction.css",
        "crwallm.crawler.extraction.structured",
        "crwallm.crawler.extraction.documents",
        "crwallm.crawler.fetching.http",
        "crwallm.desktop.selftest",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# selectolax ships the Cython sources next to the compiled extensions, and the
# collector takes the whole directory - 5 MB of .c nobody will ever compile.
a.datas = [entry for entry in a.datas if not entry[0].endswith((".c", ".pyx", ".pxd"))]

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CRWALLM",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # False, and this is the whole point of the packaging work: a console here
    # means a black window behind the app that someone eventually closes,
    # taking the app with it.
    console=False,
    disable_windowed_traceback=False,
    icon=str(HERE / "crwallm.ico"),
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="CRWALLM",
)
