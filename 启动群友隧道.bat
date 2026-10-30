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

rem ---- start clean: kill every cloudflared and drop the stale URL ------
rem Closing this window is a hard kill, so friend_tunnel.py never runs its
rem cleanup and state\friend_url.txt keeps a hostname that is already dead.
rem Two tunnels at once also means two random hostnames, and Cloudflare
rem usually refuses the second one. The symptom is a 530 page behind a link
rem that still looks perfectly normal - so clean up before every start.
taskkill /F /IM cloudflared.exe >nul 2>&1
if %errorlevel% equ 0 (echo [OK] stopped every old cloudflared) else (echo [OK] no cloudflared was running)
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
