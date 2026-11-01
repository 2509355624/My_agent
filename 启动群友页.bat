@echo off
setlocal
cd /d "%~dp0"
title FRIEND-WEB

rem Public-facing LoRA preset page for group members (port 5175).
rem Depends on the main agent web (5174): it proxies auth + add-job calls there.
rem Keep this file pure ASCII so cmd never mis-renders it.

rem ---- pick python: launcher env > project venv > known venvs > PATH ----
set "PY="
if defined AGENT_PY if exist "%AGENT_PY%" set "PY=%AGENT_PY%"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0venv\Scripts\python.exe" set "PY=%~dp0venv\Scripts\python.exe"
if not defined PY if exist "D:\AI\my_env\Scripts\python.exe" set "PY=D:\AI\my_env\Scripts\python.exe"
if not defined PY if exist "D:\AI\confyui_env\Scripts\python.exe" set "PY=D:\AI\confyui_env\Scripts\python.exe"
if not defined PY if exist "D:\AI\comfy_env\Scripts\python.exe" set "PY=D:\AI\comfy_env\Scripts\python.exe"
if not defined PY set "PY=python"

echo ==================================================
echo   FRIEND WEB  -  5175
echo ==================================================
echo.

rem ---- start clean: kill whatever already holds 5175 ------------------
rem Double-clicking this bat while an older FRIEND-WEB is still running
rem does NOT replace it on Windows. Werkzeug sets SO_REUSEADDR, so the
rem second python binds 5175 without any error and prints "Running on
rem http://127.0.0.1:5175" - but the FIRST process keeps winning every
rem connection, so the new one gets zero requests and the browser still
rem talks to the old code. Measured 2026-11-01: old window alive since
rem 18:27, a fresh one started at 22:25 logged no request at all, and
rem /workshop stayed 404 on all 8 tries. The window looks healthy, which
rem is exactly why a code change seems to never take effect.
rem So: kill the listener first, wait for the socket, then start.
set "OLDPID="
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":5175 " ^| findstr "LISTENING"') do set "OLDPID=%%a"
if not defined OLDPID (
    echo [OK] 5175 was free
) else (
    taskkill /F /PID %OLDPID% >nul 2>&1
    echo [OK] stopped old FRIEND-WEB ^(pid %OLDPID%^)
)
rem taskkill returns before the process lets go of its socket. Wait for
rem the port to actually be free, max 10s so a stuck one cannot hang here.
set "WAITN=0"
:WAIT_OLD
netstat -ano 2>nul | findstr ":5175 " | findstr "LISTENING" >nul
if errorlevel 1 goto PORT_FREE
set /a WAITN+=1
if %WAITN% GEQ 20 goto PORT_FREE
ping -n 1 -w 300 127.0.0.1 >nul
goto WAIT_OLD
:PORT_FREE
echo.

rem ---- backend must be up first, otherwise every call returns 502 ----
set "BACKEND_OK="
for /f %%a in ('netstat -ano 2^>nul ^| findstr ":5174 " ^| findstr "LISTENING"') do set "BACKEND_OK=1"
if not defined BACKEND_OK (
    echo [NOTE] 5174 is not listening yet.
    echo        If you just ran the all-in-one launcher, it is probably still
    echo        booting - leave this window open, it connects per request.
    echo        Only if 5174 never comes up, start the main agent web first.
    echo.
)

echo   local :  http://127.0.0.1:5175
echo   public:  double-click the friend-tunnel bat
echo            ^(it writes state\friend_url.txt, bot replies /preset-web^)
echo   quit  :  close this window
echo.
echo ==================================================
echo.

"%PY%" friend_app.py

echo.
echo [FRIEND-WEB] process exited, code %errorlevel%
pause
