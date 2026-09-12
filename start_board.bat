@echo off
rem ===== Bousai board launcher =====
cd /d "%~dp0"

rem 1) start the data server (minimized, restarts automatically)
start "bousai_board_server" /min cmd /c run_server.bat

rem 2) wait until the server is ready
rem     Poll until it answers. A fixed wait can be too short right after a
rem     PC reboot, and the browser then sits on an error page for good:
rem     the page never loads, so its auto-refresh never runs.
set /a _try=0
:wait
curl -s -o nul --connect-timeout 1 http://127.0.0.1:8765/api/config && goto ready
set /a _try+=1
if %_try% geq 60 goto ready
timeout /t 1 /nobreak >nul
goto wait
:ready

rem 3) open the board full-screen with Edge (kiosk mode)
start "" msedge --kiosk http://127.0.0.1:8765/ --edge-kiosk-type=fullscreen --no-first-run

rem Chrome instead of Edge: remove "rem" from the next line and add "rem" to the Edge line
rem start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --kiosk --noerrdialogs --disable-session-crashed-bubble http://127.0.0.1:8765/
