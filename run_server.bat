@echo off
rem Start the identifiable supervisor so stop_boards.bat can stop restarts too.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0launch_boards.py" --server bousai
) else (
  python "%~dp0launch_boards.py" --server bousai
)
if errorlevel 1 pause
