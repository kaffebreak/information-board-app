@echo off
rem Start enabled boards. Each browser waits for its own server independently.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0launch_boards.py"
) else (
  python "%~dp0launch_boards.py"
)
if errorlevel 1 pause
