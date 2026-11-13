@echo off
rem AdBeam Leads: local launcher. Creates .venv on first run, starts the web UI, opens browser.
rem Keep this file ASCII-only with CRLF line endings (cmd.exe misparses UTF-8/LF batch files).
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto run

echo [setup] First run: creating Python 3.12 environment...
py -3.12 -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -e ".[dev]"
if errorlevel 1 goto fail

:run
set OPEN_FLAG=--open
if defined ADS_LEADS_NO_OPEN set OPEN_FLAG=
echo [run] AdBeam Leads: http://127.0.0.1:8000  (close this window or press Ctrl+C to stop)
".venv\Scripts\python.exe" -m ads_leads.cli web %OPEN_FLAG%
if errorlevel 1 goto fail
goto end

:fail
echo.
echo [error] Something went wrong, see messages above.
echo         Python 3.12 is required (check: py -3.12 --version).
pause

:end
endlocal
