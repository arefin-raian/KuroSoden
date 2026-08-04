@echo off
REM ============================================================
REM  Kuro Soden launcher (Windows)
REM  Double-click this file, or run `run.bat` from a terminal.
REM
REM  Startup order:
REM    1. Use .venv inside this repo if it already exists.
REM    2. If missing, clone the parent NekoFetch .venv without downloading deps.
REM    3. Only if no parent venv exists, try to create a fresh venv.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "PARENT_VENV=..\.venv"
set "PARENT_VENV_PY=%PARENT_VENV%\Scripts\python.exe"
set "READY_MARKER=%VENV_DIR%\.kurosoden-ready"

REM --- ensure a virtual environment exists --------------------
if not exist "%VENV_PY%" (
    if exist "%PARENT_VENV_PY%" (
        echo [Kuro Soden] No local .venv found. Copying parent NekoFetch .venv ...
        robocopy "%PARENT_VENV%" "%VENV_DIR%" /E /NFL /NDL /NJH /NJS /NP >nul
        if errorlevel 8 (
            echo [Kuro Soden] ERROR: could not copy parent virtual environment.
            pause
            exit /b 1
        )
    ) else (
        echo [Kuro Soden] No local or parent virtual environment found. Creating .venv ...
        py -3.12 -m venv "%VENV_DIR%" 2>nul || python -m venv "%VENV_DIR%"
    )

    if not exist "%VENV_PY%" (
        echo [Kuro Soden] ERROR: could not prepare a virtual environment.
        echo [Kuro Soden] Parent venv was not usable and Python was not found on PATH.
        pause
        exit /b 1
    )
)

REM --- register this repo without downloading dependencies -----
if not exist "%READY_MARKER%" (
    echo [Kuro Soden] Registering local package without downloading dependencies ...
    "%VENV_PY%" -m pip install -e . --no-deps
    if errorlevel 1 (
        echo [Kuro Soden] ERROR: local package registration failed.
        echo [Kuro Soden] The copied venv exists, but pip could not install this repo.
        pause
        exit /b 1
    )
    echo ready>"%READY_MARKER%"
)

REM --- ensure the Playwright Chromium browser is installed -----
REM Thumbnail rendering (Senku) needs a headless Chromium; the pip package alone
REM doesn't ship the browser binary. "playwright install" is idempotent, so this
REM is a fast no-op once installed. Non-fatal: the bots still start if it fails
REM (thumbnails just won't render until it succeeds). Windows needs no separate
REM system-deps step.
set "PW_MARKER=%VENV_DIR%\.playwright-ready"
if not exist "%PW_MARKER%" (
    echo [Kuro Soden] Ensuring Playwright Chromium is installed ...
    "%VENV_PY%" -m playwright install chromium
    if errorlevel 1 (
        echo [Kuro Soden] WARNING: 'playwright install chromium' failed - thumbnail
        echo [Kuro Soden]          rendering may not work until it succeeds.
    ) else (
        echo ready>"%PW_MARKER%"
    )
)

REM --- ensure a 7-Zip binary is available (DDL rar/7z extraction) ----------
REM DDL archive links unpack .rar/.7z via the 7-Zip CLI (stdlib handles .zip).
REM The source locates 7z.exe on PATH or under tools\, so we make sure one of
REM those holds it: reuse an existing install, else winget it, then copy the
REM binary (+ its DLL) into tools\. Non-fatal - only rar/7z DDL links need it.
set "SEVENZIP_MARKER=%VENV_DIR%\.7zip-ready"
if not exist "%SEVENZIP_MARKER%" (
    where 7z.exe >nul 2>&1
    if not errorlevel 1 (
        echo ready>"%SEVENZIP_MARKER%"
    ) else if exist "tools\7z.exe" (
        echo ready>"%SEVENZIP_MARKER%"
    ) else if exist "%ProgramFiles%\7-Zip\7z.exe" (
        echo [Kuro Soden] Bundling installed 7-Zip into tools\ ...
        if not exist "tools" mkdir "tools"
        copy /Y "%ProgramFiles%\7-Zip\7z.exe" "tools\" >nul
        copy /Y "%ProgramFiles%\7-Zip\7z.dll" "tools\" >nul 2>&1
        echo ready>"%SEVENZIP_MARKER%"
    ) else (
        echo [Kuro Soden] Installing 7-Zip via winget ^(for DDL rar/7z extraction^) ...
        winget install -e --id 7zip.7zip --accept-source-agreements --accept-package-agreements >nul 2>&1
        if exist "%ProgramFiles%\7-Zip\7z.exe" (
            if not exist "tools" mkdir "tools"
            copy /Y "%ProgramFiles%\7-Zip\7z.exe" "tools\" >nul
            copy /Y "%ProgramFiles%\7-Zip\7z.dll" "tools\" >nul 2>&1
            echo ready>"%SEVENZIP_MARKER%"
        ) else (
            echo [Kuro Soden] NOTE: couldn't auto-install 7-Zip. DDL .rar/.7z links need it
            echo [Kuro Soden]       ^(.zip works without it^). Install from https://www.7-zip.org
        )
    )
)

REM --- sanity: secrets file -----------------------------------
if not exist ".env" (
    echo [Kuro Soden] WARNING: .env not found.
    echo [Kuro Soden] Copy .env.example to .env and fill in your tokens:
    echo [Kuro Soden]     copy .env.example .env
    echo.
)

if /I "%~1"=="--check" (
    echo [Kuro Soden] Launcher check passed.
    echo [Kuro Soden] Using "%VENV_PY%".
    exit /b 0
)

REM --- run ----------------------------------------------------
echo [Kuro Soden] Starting 4-bot pipeline...
echo [Kuro Soden]   Lelouch    - Request intake
echo [Kuro Soden]   Levi       - Download delegation
echo [Kuro Soden]   Senku      - Distribution
echo [Kuro Soden]   Gojo       - Publishing
echo.
echo [Kuro Soden] Press Ctrl+C to stop.

set "PATH=%~dp0%VENV_DIR%\Scripts;%PATH%"
"%VENV_PY%" main.py

echo.
echo [Kuro Soden] Process exited.
pause
