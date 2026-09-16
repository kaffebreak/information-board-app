@echo off
rem Stop only this project's launchers, servers and dedicated Edge windows.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0stop_boards.py"
) else (
  python "%~dp0stop_boards.py"
)
if errorlevel 1 pause
