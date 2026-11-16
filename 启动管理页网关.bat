@echo off
setlocal
cd /d "%~dp0"
title ADMIN-WEB

rem Admin gateway for 5174 (port 5176). Login page + reverse proxy.
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
echo   ADMIN GATEWAY  -  5176
echo ==================================================
echo.

rem ---- credentials check: no account = nobody can log in ----
"%PY%" -c "from app import admin_auth; assert admin_auth.configured()" >nul 2>&1
if errorlevel 1 (
    echo [WARN] no admin account set yet.
    echo        run: python tools/admin_passwd.py
    echo        Using an empty credential store means login is locked.
    echo.
)

rem ---- start clean: kill whatever already holds 5176 ------------------
set "OLDPID="
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":5176 " ^| findstr "LISTENING"') do set "OLDPID=%%a"
if not defined OLDPID (
    echo [OK] 5176 was free
) else (
    taskkill /F /PID %OLDPID% >nul 2>&1
    echo [OK] stopped old ADMIN-WEB ^(pid %OLDPID%^)
)
set "WAITN=0"
:WAIT_OLD
netstat -ano 2>nul | findstr ":5176 " | findstr "LISTENING" >nul
if errorlevel 1 goto PORT_FREE
set /a WAITN+=1
if %WAITN% GEQ 20 goto PORT_FREE
ping -n 1 -w 300 127.0.0.1 >nul
goto WAIT_OLD
:PORT_FREE

rem ---- backend 5174 must be up first ----
set "BACKEND_OK="
for /f %%a in ('netstat -ano 2^>nul ^| findstr ":5174 " ^| findstr "LISTENING"') do set "BACKEND_OK=1"
if not defined BACKEND_OK (
    echo [NOTE] 5174 is not listening yet.
    echo        If launched from the all-in-one script it is still booting.
    echo        The gateway connects per request, so leave this window open.
    echo.
)

echo   local :  http://127.0.0.1:5176
echo   public:  double-click the admin-tunnel bat
echo            ^(it writes state\admin_url.txt^)
echo   quit  :  close this window
echo.
echo ==================================================.
echo.

"%PY%" admin_app.py

echo.
echo [ADMIN-WEB] process exited, code %errorlevel%
pause
