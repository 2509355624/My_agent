@echo off
setlocal
cd /d "%~dp0"
title RESTART-QQBOT

set "BOT_BAT=%~dp0启动QQ机器人.bat"
set "PID_FILE=%~dp0.qq_bot.pid"

echo ==================================================
echo   重启 QQ 适配层（只动这一个进程）
echo ==================================================
echo.
echo   不碰: QQ 客户端 / SnowLuma / NapCat / ComfyUI / 看门狗
echo   协议端还在跑，所以不用重新扫码 —— 适配层起来会自己连回去。
echo.

if not exist "%BOT_BAT%" (
    echo [ERROR] 找不到 "%BOT_BAT%"
    pause
    exit /b 1
)

echo [1/3] 找旧的适配层
echo.
rem [!!] 这里**绝不能按窗口标题杀进程**。本机这些窗口是 Windows Terminal
rem      托管的，一个 WindowsTerminal.exe 里挂着好几个标签页 —— 实测
rem      QQBOT-ADAPTER 和 SNOWLUMA 就是同一个进程（标题随当前标签页变），
rem      按标题杀会把协议端一起端掉，那就得重新扫码了。
rem      所以只认 PID：优先读 .qq_bot.pid（qq_bot 启动时写的），
rem      没有就退回「谁连着协议端 3001 的 WS」。
rem      顺带：本机**没有 wmic**（Win11 24H2 起已移除），别指望按命令行查。
set "BOT_PID="
set "CMD_PID="
if not exist "%PID_FILE%" goto :by_conn
for /f "tokens=1,2" %%a in ('type "%PID_FILE%"') do (
    set "BOT_PID=%%a"
    set "CMD_PID=%%b"
)
echo       读到 PID 文件：%PID_FILE%
if not defined BOT_PID goto :by_conn

rem PID 可能已经过期（进程早退了，号被系统回收给了别人）。杀之前先确认
rem 它还是 python.exe —— 不是就整个作废，宁可开新窗口也别误杀。
tasklist /FI "PID eq %BOT_PID%" /FO CSV /NH 2>nul | findstr /i /c:"python.exe" >nul
if not errorlevel 1 goto :have_pid
echo       PID %BOT_PID% 现在不是 python 了（多半已退出），忽略
set "BOT_PID="
set "CMD_PID="

:by_conn
if defined BOT_PID goto :have_pid
echo       没有可用的 PID 文件，改按 3001 的连接找
set "PROTO_PID="
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":3001 " ^| findstr "LISTENING"') do set "PROTO_PID=%%a"
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":3001 " ^| findstr "ESTABLISHED"') do (
    if not "%%a"=="%PROTO_PID%" set "BOT_PID=%%a"
)

:have_pid
if not defined BOT_PID goto :no_pid
echo       结束适配层 PID %BOT_PID%
taskkill /PID %BOT_PID% /T /F >nul 2>&1
ping -n 3 127.0.0.1 >nul
rem 父进程是 cmd.exe 才顺手收掉 —— 结束它才能把那个标签页关掉；
rem 不是（比如从编辑器里跑的）就留着，别误伤。
if not defined CMD_PID goto :killed
tasklist /FI "PID eq %CMD_PID%" /FO CSV /NH 2>nul | findstr /i /c:"cmd.exe" >nul
if errorlevel 1 goto :killed
echo       顺手关掉承载它的 cmd 窗口 PID %CMD_PID%
taskkill /PID %CMD_PID% /T /F >nul 2>&1
:killed
echo       完成
echo.
goto :start_new

:no_pid
echo.
echo [WARN] 没找到正在跑的适配层进程 —— 本次不杀任何东西，直接开新窗口。
echo        若新窗口提示「已有实例在运行」，说明旧进程不是这个脚本能认出来的，
echo        手动关掉旧的 QQBOT-ADAPTER 标签页，再跑一次这个脚本即可。
echo.

:start_new
echo [2/3] 启动新的适配层
start "QQBOT-ADAPTER" cmd /k "%BOT_BAT%"
ping -n 4 127.0.0.1 >nul
echo       新窗口已拉起
echo.

echo [3/3] 看新窗口里有没有报错
echo       正常的话会看到连上协议端的日志。
echo       （第一次用这个脚本时，旧标签页可能还停在提示符上，
echo         手动关掉它就行，不影响新实例。）
echo.
echo ==================================================
echo   本窗口可以关；QQBOT-ADAPTER 那个窗口请勿关闭。
echo ==================================================
echo.
pause
