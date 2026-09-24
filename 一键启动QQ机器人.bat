@echo off
cd /d "%~dp0"
title QQ 机器人 - 一键启动

set "NAPCAT_BAT=D:\AI\NapCat\启动NapCat.bat"
set "BOT_BAT=D:\AI\agent_my_test\启动QQ机器人.bat"

echo ==================================================
echo   QQ 机器人 一键启动
echo ==================================================
echo.
echo   流程: 清理旧进程 -^> 启动 NapCat -^> 启动 QQ 适配层
echo.
echo   【注意】会强制结束 QQ 客户端
echo           NapCat 注入在 QQ 进程里，运行期不能同时开 QQ
echo.

if not exist "%NAPCAT_BAT%" (
    echo [ERROR] 找不到 "%NAPCAT_BAT%"
    pause
    exit /b 1
)
if not exist "%BOT_BAT%" (
    echo [ERROR] 找不到 "%BOT_BAT%"
    pause
    exit /b 1
)

echo [1/2] 清理旧进程
for %%p in (6099 3000 3001) do (
    for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":%%p " ^| findstr "LISTENING"') do (
        echo       结束端口 %%p 的进程 PID %%a
        taskkill /PID %%a /F >nul 2>&1
    )
)
taskkill /IM QQ.exe /F >nul 2>&1
taskkill /IM NapCatWinBootMain.exe /F >nul 2>&1
tasklist /FI "WINDOWTITLE eq QQBOT-ADAPTER" 2>nul | findstr /i "cmd.exe" >nul
if not errorlevel 1 (
    taskkill /FI "WINDOWTITLE eq QQBOT-ADAPTER" /T /F >nul 2>&1
    echo       QQ 适配层 : 已结束旧窗口
)
ping -n 4 127.0.0.1 >nul
echo       完成
echo.

echo [2/2] 启动 NapCat 和 QQ 适配层
start "NapCat - QQ 协议端" cmd /k "%NAPCAT_BAT%"
ping -n 4 127.0.0.1 >nul
start "QQBOT-ADAPTER" cmd /k "%BOT_BAT%"
echo       两个新窗口已启动
echo.

echo ==================================================
echo   若 NapCat 窗口提示需要扫码:
echo     浏览器打开  http://127.0.0.1:6099/webui
echo     用手机 QQ 扫码（建议用小号）
echo   登录成功后适配层会自动连上，不用重启任何东西。
echo ==================================================
echo.
echo   NapCat / QQ 适配层 两个窗口请勿关闭，本窗口可关。
echo.
pause
