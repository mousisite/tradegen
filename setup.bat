@echo off
REM Recreates the self-contained Python runtime in .runtime\python.
REM Nothing touches the system PATH or registry; delete .runtime to undo.
setlocal
set "HERE=%~dp0"
set "RT=%HERE%.runtime"
set "PY=%RT%\python"
set "PYVER=3.11.9"

echo Creating a local Python runtime in %PY%
if not exist "%RT%" mkdir "%RT%"

REM The folder reaches PowerShell as a variable, not inside quotes, so a name
REM with an apostrophe in it works.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue';" ^
  "$zip = Join-Path $env:TEMP 'stockbot-python.zip';" ^
  "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-amd64.zip' -OutFile $zip -UseBasicParsing;" ^
  "if (Test-Path -LiteralPath $env:PY) { Remove-Item -Recurse -Force -LiteralPath $env:PY };" ^
  "Expand-Archive -Path $zip -DestinationPath $env:PY -Force;" ^
  "$pth = Join-Path $env:PY 'python311._pth';" ^
  "(Get-Content $pth) -replace '^#\s*import site','import site' | Set-Content $pth -Encoding ascii;" ^
  "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile (Join-Path $env:PY 'get-pip.py') -UseBasicParsing"

if errorlevel 1 (
  echo Could not set up the Python runtime. Check your internet connection.
  exit /b 1
)

"%PY%\python.exe" "%PY%\get-pip.py" --no-warn-script-location
if errorlevel 1 (
  echo Could not install pip. Check your internet connection, then run setup.bat again.
  exit /b 1
)

REM From the lock file, as the server does: exactly the versions that were tested.
"%PY%\python.exe" -m pip install --no-warn-script-location --disable-pip-version-check --require-hashes --no-deps -r "%HERE%requirements.lock"
if errorlevel 1 (
  echo Could not install the packages. Check your internet connection, then run setup.bat again.
  exit /b 1
)

echo.
echo Setup complete. Try:
echo   run.bat --symbol AAPL
