@echo off
REM Double-click this to open Orenth in your browser.
REM It starts a small local web server and opens the page for you.
title Orenth - server
cd /d "%~dp0"

if not exist "%~dp0.runtime\python\python.exe" (
  echo.
  echo   The bundled Python runtime is missing.
  echo   Run setup.bat once to rebuild it, then start this again.
  echo.
  pause
  exit /b 1
)

echo.
echo   Starting Orenth...
echo   Your browser will open in a moment.
echo.
echo   Keep this window open while you use it.
echo   Close it, or press Ctrl+C, to stop the server.
echo.

"%~dp0.runtime\python\python.exe" "%~dp0web.py" %*

REM If the server stopped because of an error, keep the message on screen.
if errorlevel 1 pause
