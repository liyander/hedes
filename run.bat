@echo off
rem H.E.D.E.S - one-click launcher for Windows.
rem Usage:  run.bat            (port 8000)
rem         run.bat 9000       (custom port)
rem         run.bat test       (run the test suite instead)
setlocal EnableExtensions
cd /d "%~dp0"

set "PORT=8000"
if not "%~1"=="" if /i not "%~1"=="test" set "PORT=%~1"

rem --- 1. Find Python 3.10+ -------------------------------------------------
set "PY="
where py >nul 2>&1 && py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [H.E.D.E.S] Python 3.10 or newer is required. Install it from https://www.python.org/downloads/
  echo             and tick "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)

rem --- 2. Virtual environment + dependencies (reinstalled only when requirements change)
set "VENV=backend\.venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo [H.E.D.E.S] Creating virtual environment in %VENV% ...
  %PY% -m venv "%VENV%" || goto :fail
)
set "VPY=%VENV%\Scripts\python.exe"
fc /b "backend\requirements.txt" "%VENV%\requirements.installed" >nul 2>&1
if errorlevel 1 (
  echo [H.E.D.E.S] Installing dependencies ...
  "%VPY%" -m pip install --disable-pip-version-check -q --upgrade pip
  "%VPY%" -m pip install --disable-pip-version-check -q -r backend\requirements.txt || goto :fail
  copy /y "backend\requirements.txt" "%VENV%\requirements.installed" >nul
)

if /i "%~1"=="test" (
  pushd backend
  "..\%VPY%" -m pytest tests -q
  set "RC=%ERRORLEVEL%"
  popd
  pause
  exit /b %RC%
)

rem --- 3. Configuration (values in .env are used unless overridden here) ---
if not exist ".env" if exist ".env.example" copy /y ".env.example" ".env" >nul
if not defined SEED_PASSWORD (
  findstr /r /c:"^SEED_PASSWORD=." ".env" >nul 2>&1 || set "SEED_PASSWORD=IntegraDemo2026"
)
rem The bundled mock APIs live on this server, so the public URL must match the port.
if not defined PUBLIC_BASE_URL set "PUBLIC_BASE_URL=http://127.0.0.1:%PORT%"

rem --- 4. Port check, open the browser once the server answers, run --------
netstat -ano | findstr /r /c:"127.0.0.1:%PORT% .*LISTENING" /c:"0.0.0.0:%PORT% .*LISTENING" >nul
if not errorlevel 1 (
  echo [H.E.D.E.S] Port %PORT% is already in use. Stop the other program or run:  run.bat 8001
  pause
  exit /b 1
)

echo.
echo   H.E.D.E.S  ^>  %PUBLIC_BASE_URL%
echo   Accounts   ^>  builder / admin / owner
if defined SEED_PASSWORD (echo   Password   ^>  %SEED_PASSWORD%) else (echo   Password   ^>  SEED_PASSWORD in .env)
echo   API docs   ^>  %PUBLIC_BASE_URL%/docs
echo   Stop       ^>  Ctrl+C
echo.

if not defined NO_BROWSER start "" /b powershell -NoProfile -Command "for($i=0;$i -lt 60;$i++){try{Invoke-WebRequest -UseBasicParsing '%PUBLIC_BASE_URL%/api/v1/health' -TimeoutSec 1 | Out-Null; Start-Process '%PUBLIC_BASE_URL%'; break}catch{Start-Sleep -Milliseconds 500}}"

pushd backend
"..\%VPY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
popd
exit /b 0

:fail
echo [H.E.D.E.S] Setup failed - see the messages above.
pause
exit /b 1
