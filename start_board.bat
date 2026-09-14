@echo off
rem ===== Bousai board launcher =====
cd /d "%~dp0"

rem 0) use the same Python as run_server.bat
where py >nul 2>nul
if %errorlevel%==0 (set _PY=py -3) else (set _PY=python)

rem 1) the server takes "port" from config.json, so read it here too.
rem    Keep 8765 if config.json is missing or unreadable.
set PORT=8765
for /f "usebackq delims=" %%p in (`%_PY% -c "import json,os;f='config.json';c=json.load(open(f,encoding='utf-8-sig')) if os.path.exists(f) else {};print(int(c.get('port',8765)))" 2^>nul`) do set PORT=%%p

rem 2) start the data server (minimized, restarts automatically)
start "bousai_board_server" /min cmd /c run_server.bat

rem 3) wait until the server answers. Never give up: opening the browser before
rem    the server is up leaves it on an error page for good, because the page
rem    never loads and so its auto-refresh never runs. run_server.bat restarts
rem    the server by itself, so keep polling and open only once it answers.
set /a _try=0
:wait
curl -s -o nul --connect-timeout 1 http://127.0.0.1:%PORT%/api/config && goto ready
set /a _try+=1
set /a _msg=_try %% 15
if %_msg%==0 echo Waiting for the server on port %PORT% ... (%_try%s)
timeout /t 1 /nobreak >nul
goto wait
:ready

rem 4) open the board full-screen with Edge (kiosk mode)
start "" msedge --kiosk http://127.0.0.1:%PORT%/ --edge-kiosk-type=fullscreen --no-first-run

rem Chrome instead of Edge: remove "rem" from the next line and add "rem" to the Edge line
rem start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --kiosk --noerrdialogs --disable-session-crashed-bubble http://127.0.0.1:%PORT%/
