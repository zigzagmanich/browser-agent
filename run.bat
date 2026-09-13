@echo off
rem Windows: double-click opens the agent.
rem Prefer Windows Terminal: the legacy console renders Cyrillic with wide gaps
rem and redraws deletions incorrectly. WT_SESSION is set inside Windows Terminal.
if not defined WT_SESSION (
  where wt >nul 2>&1 && (
    start "" wt -d "%~dp0." cmd /c "%~f0" %*
    exit /b
  )
)
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" (set "PY=venv\Scripts\python.exe") else (set "PY=python")
"%PY%" main.py %*
pause
