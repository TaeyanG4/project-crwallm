@echo off
rem ---------------------------------------------------------------------------
rem  CRWALLM launcher. Double-click this.
rem
rem  ASCII only, on purpose. cmd parses a .bat using the system codepage before
rem  anything in it runs, so UTF-8 Korean here is read as byte soup and its
rem  fragments are executed as commands - `chcp 65001` on line two does not
rem  save you, because the damage happens at parse time. Every message the user
rem  is meant to read comes from Python, which controls its own encoding.
rem
rem  Two things a double-clicked script must do that a typed command need not:
rem  run from its own directory (Explorer starts it wherever it likes), and
rem  hold the window open on failure (or the error flashes past).
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

if not exist ".venv\Scripts\crwallm.exe" (
    echo.
    echo   First run - installing dependencies. This takes a minute.
    echo.
    uv sync
    if errorlevel 1 (
        echo.
        echo   Dependency install failed. See the messages above.
        pause
        exit /b 1
    )
)

uv run crwallm up --launcher
set EXIT_CODE=%errorlevel%

rem 0 is a clean stop and 130 is Ctrl-C. Holding the window open for either
rem just gives the user a second window to close.
if "%EXIT_CODE%"=="0" exit /b 0
if "%EXIT_CODE%"=="130" exit /b 0

echo.
pause
exit /b %EXIT_CODE%
