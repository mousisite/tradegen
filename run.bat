@echo off
REM Runs the analyser using the Python runtime bundled in .runtime\python.
REM Nothing is installed system-wide, so this works even with no Python on PATH.
setlocal
set "HERE=%~dp0"
if not exist "%HERE%.runtime\python\python.exe" (
  echo The bundled Python runtime is missing.
  echo Run setup.bat once to recreate it.
  exit /b 1
)
"%HERE%.runtime\python\python.exe" "%HERE%analyze.py" %*
