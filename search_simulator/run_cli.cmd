@echo off
setlocal

chcp 65001 > nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_ROOT=%%~fI"
set "VENV_ACTIVATE=%PROJECT_ROOT%\.venv\Scripts\activate.bat"

if not exist "%VENV_ACTIVATE%" (
    echo ??????????? "%VENV_ACTIVATE%"
    exit /b 1
)

call "%VENV_ACTIVATE%"
set "PYTHONPATH=%PROJECT_ROOT%"
cd /d "%SCRIPT_DIR%"

python -m search_simulator --cli %*
