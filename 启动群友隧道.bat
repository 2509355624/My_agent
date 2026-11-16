@echo off
setlocal
cd /d "%~dp0"
title FRIEND-TUNNEL

rem Cloudflare Quick Tunnel -> friend page on port 5175.
rem Writes the random public URL into state\friend_url.txt so the bot can
rem answer the /preset-web command with a live link.
rem Free: no account, no domain, no credit card. The URL changes on restart.
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
rem Two tunnels run at once now (friend 5175 + admin 5176). Never use
rem `taskkill /IM cloudflared.exe` here - that would kill the admin tunnel
rem too. friend_tunnel.py writes this tunnel's PID to state\friend_url.pid;
rem read it and stop just that one. Each tunnel owns its own pid file.
set "OLDTUN="
if exist "state\friend_url.pid" (
    set /p OLDTUN=<"state\friend_url.pid"
)
if defined OLDTUN (
    taskkill /F /PID %OLDTUN% >nul 2>&1
    if errorlevel 1 (echo [OK] old friend tunnel pid %OLDTUN% was gone) else (echo [OK] stopped old friend tunnel pid %OLDTUN%)
    del /q "state\friend_url.pid" >nul 2>&1
) else (
    echo [OK] no old friend tunnel recorded
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
echo [WARN] old friend tunnel still alive after 10s - starting anyway
:CF_GONE
if exist "state\friend_url.txt" (
    del /q "state\friend_url.txt" >nul 2>&1
    echo [OK] stale URL cleared - a new one is printed below
)
echo.

rem ---- friend page must be listening, otherwise the tunnel leads nowhere ----
set "WEB_OK="
for /f %%a in ('netstat -ano 2^>nul ^| findstr ":5175 " ^| findstr "LISTENING"') do set "WEB_OK=1"
if not defined WEB_OK (
    echo [NOTE] 5175 is not listening yet.
    echo        Start "friend web" first, or the tunnel will expose nothing.
    echo.
)

"%PY%" friend_tunnel.py

echo.
echo [FRIEND-TUNNEL] process exited, code %errorlevel%
pause
