@echo off
rem ===== Bousai board: data server (auto restart) =====
cd /d "%~dp0"
:loop
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 bousai_board.py
) else (
  python bousai_board.py
)
echo Server stopped. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
