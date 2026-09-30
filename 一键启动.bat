@echo off
cd /d "%~dp0"
rem --- venv python auto-detect: desktop my_env / laptop confyui_env ---
set "PY="
if not defined PY if exist "D:\AI\my_env\Scripts\python.exe" set "PY=D:\AI\my_env\Scripts\python.exe"
if not defined PY if exist "D:\AI\confyui_env\Scripts\python.exe" set "PY=D:\AI\confyui_env\Scripts\python.exe"
if not defined PY if exist "D:\AI\comfy_env\Scripts\python.exe" set "PY=D:\AI\comfy_env\Scripts\python.exe"
if not defined PY set "PY=python"

rem stable window title: the all-in-one launcher kills the old web window by it
title AGENT-WEB

echo.
echo ==================================================
echo   My Agent - Personal AI Assistant
echo ==================================================
echo.

echo [1/2] Kill old process on port 5174...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":5174" ^| findstr "LISTENING"') do (
  echo       Killing PID %%a
  taskkill /PID %%a /F >nul 2>&1
  ping -n 2 127.0.0.1 >nul
  goto start_server
)
echo       No old process

:start_server
echo.
echo [2/2] Starting agent...
start "" "http://localhost:5174"
%PY% agent.py
if errorlevel 1 (
  echo.
  echo ERROR: Failed to start agent.
)
echo.
pause
