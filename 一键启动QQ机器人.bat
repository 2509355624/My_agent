@echo off
setlocal enabledelayedexpansion
rem 收敛 PATH：第三方工具（Git 自带 GNU 版 find / timeout）会盖掉同名的系统命令
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem"
cd /d "%~dp0"
title QQ 机器人 - 一键启动

set "NAPCAT_BAT=D:\AI\NapCat\启动NapCat.bat"
set "BOT_BAT=D:\AI\agent_my_test\启动QQ机器人.bat"
set "PY=D:\AI\confyui_env\Scripts\python.exe"

echo ==================================================
echo   QQ 机器人 一键启动
echo ==================================================
echo.
echo   流程: 清理旧进程 -^> 启动 NapCat -^> 启动 QQ 适配层
echo.
echo   【注意】本脚本会强制结束 QQ 客户端
echo           NapCat 是注入在 QQ 进程里的，运行期不能同时开 QQ
echo.
echo   3 秒后自动开始，此期间按 Ctrl+C 可取消...
call :sleep 4

rem ---------------- 前置检查 ----------------
if not exist "%NAPCAT_BAT%" (
    echo [ERROR] 找不到 "%NAPCAT_BAT%"
    echo         请确认 NapCat 已解压到 D:\AI\NapCat
    pause
    exit /b 1
)
if not exist "%PY%" (
    echo [ERROR] 找不到 Python: "%PY%"
    pause
    exit /b 1
)

echo.
echo [1/4] 清理旧进程

rem -- 按监听端口反查 PID，结束旧的 NapCat / 占用端口的残留 --
for %%P in (6099 3000 3001) do call :killport %%P

rem -- QQ 客户端：NapCat 注入在 QQ.exe 内部，结束它即等于停掉 NapCat --
taskkill /IM QQ.exe /F >nul 2>&1
if errorlevel 1 (
    echo       QQ.exe : 未运行
) else (
    echo       QQ.exe : 已结束（NapCat 一并停止）
)
taskkill /IM NapCatWinBootMain.exe /F >nul 2>&1

rem -- QQ 适配层不监听端口，按窗口标题结束（/T 连它的 python 子进程一起结束）--
rem    标题由「启动QQ机器人.bat」里的 title 固定为 QQBOT-ADAPTER。
rem    为兼容「在别处手动跑过 python -m app.qq_bot」的情况，适配层自身
rem    还带一道单实例锁：真漏掉了也不会双开，只会提示已有实例在运行。
tasklist /FI "WINDOWTITLE eq QQBOT-ADAPTER" 2>nul | findstr /i "cmd.exe" >nul
if errorlevel 1 (
    echo       QQ 适配层 : 未发现旧窗口
) else (
    taskkill /FI "WINDOWTITLE eq QQBOT-ADAPTER" /T /F >nul 2>&1
    echo       QQ 适配层 : 已结束旧窗口
)

echo       等待端口释放...
call :sleep 3

echo.
echo [2/4] 启动 NapCat
start "NapCat - QQ 协议端" cmd /k "%NAPCAT_BAT%"
echo       已在新窗口启动

echo.
echo [3/4] 等待 NapCat WebUI ^(6099^) 就绪
set /a TRY=0
:wait_napcat
set "HIT="
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":6099 " ^| findstr "LISTENING"') do set "HIT=1"
if defined HIT (
    echo       就绪 ^(用时约 !TRY! 秒^)
    goto napcat_ready
)
set /a TRY+=1
if !TRY! GEQ 40 (
    echo       [超时] 40 秒内 6099 未监听
    echo              请看 NapCat 窗口的报错，首次使用需先扫码登录
    goto napcat_ready
)
call :sleep 2
goto wait_napcat

:napcat_ready
echo.
echo [4/4] 启动 QQ 适配层
rem 标题必须是 QQBOT-ADAPTER：一键脚本靠窗口标题结束上一个适配层
start "QQBOT-ADAPTER" cmd /k "%BOT_BAT%"
echo       已在新窗口启动

rem -- 3000/3001 只在 QQ 登录成功后才监听 --
set "LOGGED="
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":3001 " ^| findstr "LISTENING"') do set "LOGGED=1"

echo.
echo ==================================================
if defined LOGGED (
    echo   完成：QQ 已在线，消息链路就绪。
) else (
    echo   完成：NapCat 还需要扫码登录。
    echo.
    echo   浏览器打开:  http://127.0.0.1:6099/webui
    echo   用手机 QQ 扫码（建议用小号）
    echo.
    echo   登录成功后适配层会自动连上，不用重启任何东西。
)
echo ==================================================
echo.
echo   NapCat / QQ Bot 两个窗口请勿关闭。
echo   本窗口可以直接关掉。
echo.
call :sleep 16
exit /b 0

rem ---------------- 子过程 ----------------
:killport
set "FOUND="
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":%~1 " ^| findstr "LISTENING"') do (
    if not "%%a"=="0" (
        if not defined P_%%a (
            set "P_%%a=1"
            taskkill /PID %%a /F >nul 2>&1
            echo       端口 %~1 : 已结束 PID %%a
        )
        set "FOUND=1"
    )
)
if not defined FOUND echo       端口 %~1 : 空闲
exit /b 0

rem 用 ping 等待：%~1 为 ping 次数，实际约 %~1-1 秒
:sleep
ping -n %~1 127.0.0.1 >nul 2>&1
exit /b 0
