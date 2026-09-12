@echo off
REM Paper trading journal, using the Python runtime bundled in .runtime\python.
setlocal
set "HERE=%~dp0"
if not exist "%HERE%.runtime\python\python.exe" (
  echo The bundled Python runtime is missing.
  echo Run setup.bat once to recreate it.
  exit /b 1
)
"%HERE%.runtime\python\python.exe" "%HERE%paper.py" %*
