@echo off
setlocal
cd /d "%~dp0"
title ADMIN-TUNNEL

rem Cloudflare Quick Tunnel -> admin gateway on port 5176.
rem Reuses friend_tunnel.py with two env overrides so there is ONE tunnel
rem implementation: target 5176 and write the URL to state\admin_url.txt
rem (instead of the friend page's state\friend_url.txt).
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

rem ---- start clean: kill ONLY this tunnel's old cloudflared ------------
rem friend_tunnel.py writes this tunnel's PID to state\admin_url.pid.
rem Never `taskkill /IM cloudflared.exe` - that kills the friend tunnel too.
set "OLDTUN="
if exist "state\admin_url.pid" (
    set /p OLDTUN=<"state\admin_url.pid"
)
if defined OLDTUN (
    taskkill /F /PID %OLDTUN% >nul 2>&1
    if errorlevel 1 (echo [OK] old admin tunnel pid %OLDTUN% was gone) else (echo [OK] stopped old admin tunnel pid %OLDTUN%)
    del /q "state\admin_url.pid" >nul 2>&1
) else (
    echo [OK] no old admin tunnel recorded
)
set "WAITN=0"
:WAIT_CF
if not defined OLDTUN goto CF_GONE
tasklist /FI "PID eq %OLDTUN%" 2>nul | findstr /I "%OLDTUN%" >nul
if errorlevel 1 goto CF_GONE
set /a WAITN+=1
if %WAITN% geq 10 goto CF_STUCK
timeout /t 1 /nobreak >nul
goto WAIT_CF
:CF_STUCK
echo [WARN] old admin tunnel still alive after 10s - starting anyway
:CF_GONE
if exist "state\admin_url.txt" (
    del /q "state\admin_url.txt" >nul 2>&1
    echo [OK] stale admin URL cleared - a new one is printed below
)
echo.

rem ---- admin gateway must be listening, otherwise tunnel leads nowhere --
set "WEB_OK="
for /f %%a in ('netstat -ano 2^>nul ^| findstr ":5176 " ^| findstr "LISTENING"') do set "WEB_OK=1"
if not defined WEB_OK (
    echo [NOTE] 5176 is not listening yet.
    echo        Start "admin web" first, or the tunnel will expose nothing.
    echo        If launched from the all-in-one script it is still booting.
    echo.
)

set "FRIEND_TUNNEL_TARGET=http://127.0.0.1:5176"
set "FRIEND_TUNNEL_URL_FILE=admin_url.txt"
"%PY%" friend_tunnel.py

echo.
echo [ADMIN-TUNNEL] process exited, code %errorlevel%
pause
