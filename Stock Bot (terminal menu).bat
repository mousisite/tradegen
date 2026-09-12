@echo off
REM The text-based menu, for when you would rather not use a browser.
REM The web interface in Stock Bot.bat is the main way in.
title Stock Bot - terminal menu
cd /d "%~dp0"

if not exist "%~dp0.runtime\python\python.exe" (
  echo.
  echo   The bundled Python runtime is missing.
  echo   Run setup.bat once to rebuild it, then start this again.
  echo.
  pause
  exit /b 1
)

"%~dp0.runtime\python\python.exe" "%~dp0menu.py"

if errorlevel 1 pause
