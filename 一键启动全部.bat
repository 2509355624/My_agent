@echo off
cd /d "%~dp0"
title 全部启动 - Agent 网页 + QQ 机器人

set "WEB_BAT=D:\AI\agent_my_test\一键启动.bat"
set "NAPCAT_BAT=D:\AI\NapCat\启动NapCat.bat"
set "BOT_BAT=D:\AI\agent_my_test\启动QQ机器人.bat"

echo ==================================================
echo   全部启动 : Agent 网页 + NapCat + QQ 适配层
echo ==================================================
echo.
echo   【注意】会强制结束 QQ 客户端
echo   NapCat 注入在 QQ 进程里，运行期不能同时开 QQ
echo.

if not exist "%WEB_BAT%" (
    echo [ERROR] 找不到 "%WEB_BAT%"
    pause
    exit /b 1
)
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

rem NapCat 已在跑说明小号已登录，重启要重新扫码 -> 跳过它，只重启网页端和适配层
set "NAPCAT_RUNNING="
for /f %%a in ('netstat -ano 2^>nul ^| findstr ":6099 " ^| findstr "LISTENING"') do set "NAPCAT_RUNNING=1"

echo [1/2] 清理旧进程
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":5174 " ^| findstr "LISTENING"') do (
    echo       结束网页端 PID %%a
    taskkill /PID %%a /F >nul 2>&1
)
tasklist /FI "WINDOWTITLE eq QQBOT-ADAPTER" 2>nul | findstr /i "cmd.exe" >nul
if not errorlevel 1 (
    taskkill /FI "WINDOWTITLE eq QQBOT-ADAPTER" /T /F >nul 2>&1
    echo       结束旧的 QQ 适配层窗口
)

if defined NAPCAT_RUNNING (
    echo       NapCat 已在运行，跳过 - 重启它需要重新扫码
) else (
    for %%p in (6099 3000 3001) do (
        for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":%%p " ^| findstr "LISTENING"') do (
            echo       结束端口 %%p 的进程 PID %%a
            taskkill /PID %%a /F >nul 2>&1
        )
    )
    taskkill /IM QQ.exe /F >nul 2>&1
    taskkill /IM NapCatWinBootMain.exe /F >nul 2>&1
)
ping -n 5 127.0.0.1 >nul
echo       完成
echo.

echo [2/2] 启动
start "Agent Web" cmd /k "%WEB_BAT%"
echo       Agent Web        网页端  http://localhost:5174
if defined NAPCAT_RUNNING (
    echo       NapCat           已在运行，本次不动
) else (
    start "NapCat" cmd /k "%NAPCAT_BAT%"
    echo       NapCat           QQ 协议端
    ping -n 4 127.0.0.1 >nul
)
start "QQBOT-ADAPTER" cmd /k "%BOT_BAT%"
echo       QQBOT-ADAPTER    QQ 适配层
echo.

echo ==================================================
echo   需要扫码登录时:
echo   浏览器打开  http://127.0.0.1:6099/webui
echo   用手机 QQ 扫码，建议用小号
echo   登录成功后适配层会自动连上，无需重启任何东西
echo ==================================================
echo.
echo   新开的窗口请勿关闭。本窗口可以关。
pause
