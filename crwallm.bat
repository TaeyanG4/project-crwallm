@echo off
rem ---------------------------------------------------------------------------
rem  CRWALLM. Double-click this.
rem
rem  It opens one window and nothing else. No Docker, no database, no server,
rem  no browser - the window is the program. Docker is only ever needed for the
rem  parts that keep a history (crwallm jobs, and the web UI), and neither of
rem  those is here.
rem
rem  ASCII only, on purpose. cmd parses a .bat using the system codepage before
rem  a single line of it runs, so UTF-8 Korean here is read as byte soup and
rem  its fragments are executed as commands - `chcp 65001` on line two does not
rem  save you, because the damage is done at parse time. Every message meant
rem  for a person comes from Python, which controls its own encoding.
rem
rem  Two things a double-clicked script must do that a typed command need not:
rem  run from its own directory (Explorer starts it wherever it likes), and
rem  hold the window open on failure, or the reason flashes past unread.
rem ---------------------------------------------------------------------------

cd /d "%~dp0"
title CRWALLM

where uv >nul 2>&1
if errorlevel 1 (
    echo.
    echo   uv is not installed.
    echo.
    echo   Run this in PowerShell, then start me again:
    echo     powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    echo.
    pause
    exit /b 1
)

rem crwallm-desktop.exe, not crwallm.exe: the marker has to be the thing about
rem to be launched. A venv from an older checkout has the second and not the
rem first, and checking for the wrong one skips the sync that would create it.
if not exist ".venv\Scripts\crwallm-desktop.exe" (
    echo.
    echo   First run - installing. This takes a minute, once.
    echo.
    uv sync
    if errorlevel 1 (
        echo.
        echo   Install failed. See the messages above.
        echo.
        pause
        exit /b 1
    )
)

rem A gui-scripts entry point: it runs without a console, so `start` hands off
rem and this window closes instead of sitting behind the app waiting to be
rem closed by someone who does not know it would take the app with it.
rem Anything that goes wrong from here is a message box, not a printed line.
start "" ".venv\Scripts\crwallm-desktop.exe"
exit /b 0
