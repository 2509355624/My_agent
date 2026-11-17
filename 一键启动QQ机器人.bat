@echo off
setlocal
cd /d "%~dp0"
rem --- venv python auto-detect: desktop my_env / laptop confyui_env ---
set "PY="
if not defined PY if exist "D:\AI\my_env\Scripts\python.exe" set "PY=D:\AI\my_env\Scripts\python.exe"
if not defined PY if exist "D:\AI\confyui_env\Scripts\python.exe" set "PY=D:\AI\confyui_env\Scripts\python.exe"
if not defined PY if exist "D:\AI\comfy_env\Scripts\python.exe" set "PY=D:\AI\comfy_env\Scripts\python.exe"
if not defined PY set "PY=python"
title QQBOT-SNOWLUMA

set "SNOWLUMA_BAT=%~dp0启动SnowLuma.bat"
set "BOT_BAT=%~dp0启动QQ机器人.bat"

echo ==================================================
echo   QQ 机器人 一键启动（SnowLuma）
echo ==================================================
echo.
echo   流程: 清理旧进程 -^> 启动 SnowLuma -^> 打开 WebUI 扫码 -^> 启动 QQ 适配层
echo.
echo   【注意】会强制结束 QQ 客户端，SnowLuma 需要重新注入。
echo.

if not exist "%SNOWLUMA_BAT%" (
    echo [ERROR] 找不到 "%SNOWLUMA_BAT%"
    pause
    exit /b 1
)
if not exist "%BOT_BAT%" (
    echo [ERROR] 找不到 "%BOT_BAT%"
    pause
    exit /b 1
)

echo [1/4] 清理旧进程...
for %%P in (5099 3000 3001) do (
    for /f "tokens=5" %%A in ('netstat -ano 2^>nul ^| findstr ":%%P " ^| findstr "LISTENING"') do (
        echo       关闭端口 %%P 的旧进程 PID %%A
        taskkill /PID %%A /F >nul 2>&1
    )
)
taskkill /FI "WINDOWTITLE eq SNOWLUMA" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QQBOT-ADAPTER" /T /F >nul 2>&1
taskkill /IM QQ.exe /F >nul 2>&1

echo [2/4] 启动 SnowLuma...
start "SNOWLUMA" cmd /k "%SNOWLUMA_BAT% auto"

echo [3/4] 打开 WebUI...
start "" "http://127.0.0.1:5099"
echo       浏览器里扫码登录；没登录前 3001 不会监听，适配层会自动重试。

echo [4/4] 启动 QQ 适配层...
start "QQBOT-ADAPTER" cmd /k "%BOT_BAT%"

echo.
echo   SnowLuma WebUI: http://127.0.0.1:5099
echo   登录成功后适配层会自动连上。
echo   SNOWLUMA / QQBOT-ADAPTER 两个窗口请勿关闭，本窗口可关。
echo.
pause
