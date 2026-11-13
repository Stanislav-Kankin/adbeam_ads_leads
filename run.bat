@echo off
rem Запуск AdBeam Leads локально: создаёт окружение при первом запуске и открывает браузер.
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Первый запуск: создаю окружение Python 3.12...
    py -3.12 -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -e ".[dev]" || goto :error
)

".venv\Scripts\python.exe" -m ads_leads.cli web --open
goto :eof

:error
echo Ошибка установки. Нужен Python 3.12 (py -3.12).
pause
