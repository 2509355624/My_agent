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
