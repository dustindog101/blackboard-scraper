@echo off
rem ==============================================================================
rem Blackboard Scraper CLI Windows CMD Installer
rem Installs 'bbscraper', 'blackboard', and 'bb' into %USERPROFILE%\.local\bin
rem ==============================================================================

setlocal enabledelayedexpansion

set "PROJECT_DIR=%~dp0"
set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "BIN_DIR=%USERPROFILE%\.local\bin"
set "VENV_PYTHON=%PROJECT_DIR%\.venv\Scripts\python.exe"

echo 🎓 Installing Blackboard Scraper Global CLI on Windows...
echo    ↳ Project Directory: %PROJECT_DIR%
echo    ↳ Target Bin Dir:    %BIN_DIR%

if not exist "%VENV_PYTHON%" (
    echo ❌ Error: Virtual environment not found at %VENV_PYTHON%
    echo    Please create it first:
    echo    python -m venv .venv
    echo    .\.venv\Scripts\pip install -r requirements.txt
    echo    .\.venv\Scripts\playwright install chromium
    exit /b 1
)

if not exist "%BIN_DIR%" mkdir "%BIN_DIR%"

(
echo @echo off
echo "%VENV_PYTHON%" "%PROJECT_DIR%\main.py" %%*
) > "%BIN_DIR%\bb.cmd"

(
echo @echo off
echo "%VENV_PYTHON%" "%PROJECT_DIR%\main.py" %%*
) > "%BIN_DIR%\blackboard.cmd"

(
echo @echo off
echo "%VENV_PYTHON%" "%PROJECT_DIR%\main.py" %%*
) > "%BIN_DIR%\bbscraper.cmd"

echo ✨ Successfully installed global Windows commands:
echo    • bb.cmd         -^> %BIN_DIR%\bb.cmd
echo    • blackboard.cmd -^> %BIN_DIR%\blackboard.cmd
echo    • bbscraper.cmd  -^> %BIN_DIR%\bbscraper.cmd
echo.
echo 🚀 Make sure %BIN_DIR% is in your PATH. You can then run 'bb', 'blackboard', or 'bbscraper'!
