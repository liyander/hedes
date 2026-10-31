@echo off
rem H.E.D.E.S - one-click launcher for Windows.
rem Usage:  run.bat            (port 8000)
rem         run.bat 9000       (custom port)
rem         run.bat test       (run the test suite instead)
rem         run.bat demo       (also enable the bundled MOCK APIs and sample data - for demos only)
rem         run.bat reset      (back up the database and start empty, e.g. to drop earlier demo/mock data)
rem By default H.E.D.E.S runs against REAL services only: no mock APIs, no sample integrations.
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "PORT=8000"
set "MODE=%~1"
if not "%~1"=="" if /i not "%~1"=="test" if /i not "%~1"=="demo" if /i not "%~1"=="reset" set "PORT=%~1"

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

rem --- 2b. reset: keep a timestamped backup of the database, then start with a clean one ---
if /i "%MODE%"=="reset" (
  for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmm"') do set "STAMP=%%i"
  echo [H.E.D.E.S] This moves backend\data\*.db aside as a .bak file and starts with an EMPTY database.
  echo             Existing accounts, integrations, credentials and data will no longer appear; restore the .bak file to get them back.
  choice /m "Continue"
  if errorlevel 2 exit /b 0
  for %%F in (backend\data\*.db backend\data\*.db-wal backend\data\*.db-shm) do move /y "%%F" "%%F.bak-!STAMP!" >nul
  echo [H.E.D.E.S] Database backed up. Starting fresh.
)

rem --- 3. Configuration (values in .env are used unless overridden here) ---
if not exist ".env" if exist ".env.example" copy /y ".env.example" ".env" >nul
if not defined SEED_PASSWORD (
  findstr /r /c:"^SEED_PASSWORD=." ".env" >nul 2>&1 || set "SEED_PASSWORD=IntegraDemo2026"
)
if not defined PUBLIC_BASE_URL set "PUBLIC_BASE_URL=http://127.0.0.1:%PORT%"
rem Real services only, whatever an older .env says. "run.bat demo" is the one way to switch the mock APIs on.
set "DEMO_MODE=false"
if /i "%MODE%"=="demo" set "DEMO_MODE=true"
set "AIKEY="
if defined OPENAI_API_KEY set "AIKEY=1"
if defined ANTHROPIC_API_KEY set "AIKEY=1"
if defined GEMINI_API_KEY set "AIKEY=1"
if defined BEDROCK_MODEL_ID set "AIKEY=1"
if exist ".env" findstr /r /c:"^OPENAI_API_KEY=." /c:"^ANTHROPIC_API_KEY=." /c:"^GEMINI_API_KEY=." /c:"^BEDROCK_MODEL_ID=." ".env" >nul 2>&1 && set "AIKEY=1"

rem --- 4. Port check, open the browser once the server answers, run --------
netstat -ano | findstr /r /c:"127.0.0.1:%PORT% .*LISTENING" /c:"0.0.0.0:%PORT% .*LISTENING" >nul
if not errorlevel 1 (
  echo [H.E.D.E.S] Port %PORT% is already in use. Stop the other program or run:  run.bat 8001
  pause
  exit /b 1
)

echo.
echo   H.E.D.E.S  ^>  %PUBLIC_BASE_URL%
if /i "%DEMO_MODE%"=="true" (
  echo   Accounts   ^>  builder / admin / owner
  if defined SEED_PASSWORD (echo   Password   ^>  %SEED_PASSWORD%) else (echo   Password   ^>  SEED_PASSWORD in .env)
) else (
  echo   Accounts   ^>  first visit: create the administrator account, then add users under Users
)
echo   API docs   ^>  %PUBLIC_BASE_URL%/docs
if /i "%DEMO_MODE%"=="true" (
  echo   Mode       ^>  DEMO: bundled mock APIs and sample data are ON
) else (
  echo   Mode       ^>  REAL DATA: connects to real services only ^(Integrations - Add data source^)
)
if not defined AIKEY echo   AI         ^>  no AI key found: the assistant needs a provider - sign in as owner, open AI Providers
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
