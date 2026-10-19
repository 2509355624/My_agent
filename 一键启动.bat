@echo off
setlocal
cd /d "%~dp0"
title AGENT-WEB

echo.
echo ==================================================
echo   My Agent - Personal AI Assistant
echo ==================================================
echo.

rem ---- 找 Python：外层传入 > 项目自带 venv > 本机已有 venv > PATH ----
rem   AGENT_PY 由 一键启动全部.bat 传下来，它已经找过一次，别再重复猜
rem   D:\AI\* 是本机已有的 venv，别人机器上 if exist 不成立就自动跳过
set "PY="
if defined AGENT_PY if exist "%AGENT_PY%" set "PY=%AGENT_PY%"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0venv\Scripts\python.exe" set "PY=%~dp0venv\Scripts\python.exe"
if not defined PY if exist "D:\AI\my_env\Scripts\python.exe" set "PY=D:\AI\my_env\Scripts\python.exe"
if not defined PY if exist "D:\AI\confyui_env\Scripts\python.exe" set "PY=D:\AI\confyui_env\Scripts\python.exe"
if not defined PY if exist "D:\AI\comfy_env\Scripts\python.exe" set "PY=D:\AI\comfy_env\Scripts\python.exe"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [X] 没找到 Python。
  echo     装一个 Python 3.10 以上，安装时勾选 "Add python.exe to PATH"，
  echo     然后双击 安装依赖.bat，再回来双击这个。
  echo.
  pause
  exit /b 1
)

rem ---- 依赖自检：flask 不在就别往下跑 ----
"%PY%" -c "import flask" >nul 2>nul
if errorlevel 1 (
  echo [X] 依赖还没装 ^(缺 flask^)。
  echo     先双击 安装依赖.bat，装完再运行这个。
  echo.
  pause
  exit /b 1
)

echo [1/2] 清理占用 5174 端口的旧进程...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":5174" ^| findstr "LISTENING"') do (
  echo       结束 PID %%a
  taskkill /PID %%a /F >nul 2>&1
  ping -n 2 127.0.0.1 >nul
  goto start_server
)
echo       没有旧进程

:start_server
echo.
echo [2/2] 启动中，浏览器会自动打开 http://localhost:5174
echo       关掉这个窗口 = 停止服务。
echo.
start "" "http://localhost:5174"
"%PY%" agent.py

echo.
echo 服务已退出。日志见 logs\agent.log
pause
