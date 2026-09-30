@echo off
cd /d "%~dp0"
rem --- venv python auto-detect: desktop my_env / laptop confyui_env ---
set "PY="
if not defined PY if exist "D:\AI\my_env\Scripts\python.exe" set "PY=D:\AI\my_env\Scripts\python.exe"
if not defined PY if exist "D:\AI\confyui_env\Scripts\python.exe" set "PY=D:\AI\confyui_env\Scripts\python.exe"
if not defined PY if exist "D:\AI\comfy_env\Scripts\python.exe" set "PY=D:\AI\comfy_env\Scripts\python.exe"
if not defined PY set "PY=python"
title WATCHDOG
%PY% -m app.watchdog
echo [看门狗已退出] 退出码 %ERRORLEVEL%
pause
