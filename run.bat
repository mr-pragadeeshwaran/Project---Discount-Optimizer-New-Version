@echo off
REM ====================================================================
REM  Discount Optimiser — one-click weekly run
REM  Double-click this file. It runs the full pipeline, then opens the
REM  weekly Excel report (Summary, Glide Path, Track Record, By Product...).
REM ====================================================================
cd /d "%~dp0"
REM Prefer the project venv (pinned stack, see requirements.txt); fall back to PATH python.
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
echo Step 0 — optimizer parameter review (advisory; see DISCOUNT_PLAN\PARAMS_REVIEW.md)...
%PY% -X utf8 scripts\tracker\params_review.py
echo.
echo Running the discount optimiser pipeline...
echo.
%PY% -X utf8 pipeline.py
if errorlevel 1 (
  echo.
  echo *** Pipeline failed. Scroll up to see the error. ***
  pause
  exit /b 1
)

REM Find the newest run folder under output\runs and open its report
set "LATEST="
for /f "delims=" %%i in ('dir /b /ad /o-d "output\runs\2026*" 2^>nul') do (
  set "LATEST=%%i"
  goto :found
)
:found
if defined LATEST (
  echo.
  echo Opening report: output\runs\%LATEST%\WASTE_REINVEST_REPORT.xlsx
  start "" "output\runs\%LATEST%\WASTE_REINVEST_REPORT.xlsx"
) else (
  echo Could not locate a report folder under output\runs.
)
echo.
echo Done.
pause
