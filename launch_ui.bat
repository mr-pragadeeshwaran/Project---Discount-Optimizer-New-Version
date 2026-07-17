@echo off
REM One-click dashboard: starts the local UI server and opens the browser.
cd /d "%~dp0"
REM Prefer the project venv (pinned stack, see requirements.txt); fall back to PATH python.
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
start "" http://localhost:8765
%PY% -X utf8 ui\app.py
pause
