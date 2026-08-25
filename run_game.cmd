@echo off
setlocal

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_game.ps1" %*
set "RUN_GAME_EXIT=%ERRORLEVEL%"

if not "%RUN_GAME_EXIT%"=="0" (
    echo.
    echo run_game failed with exit code %RUN_GAME_EXIT%.
    pause
)

exit /b %RUN_GAME_EXIT%
