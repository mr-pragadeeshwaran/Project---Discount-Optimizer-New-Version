@echo off
REM One-click dashboard: starts the local UI server and opens the browser.
cd /d "%~dp0"
start "" http://localhost:8765
python -X utf8 ui\app.py
pause
