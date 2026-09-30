@echo off
cd /d "%~dp0"
rem --- venv python auto-detect: desktop my_env / laptop confyui_env ---
set "PY="
if not defined PY if exist "D:\AI\my_env\Scripts\python.exe" set "PY=D:\AI\my_env\Scripts\python.exe"
if not defined PY if exist "D:\AI\confyui_env\Scripts\python.exe" set "PY=D:\AI\confyui_env\Scripts\python.exe"
if not defined PY if exist "D:\AI\comfy_env\Scripts\python.exe" set "PY=D:\AI\comfy_env\Scripts\python.exe"
if not defined PY set "PY=python"
title LAUNCH-ALL

set "WEB_BAT=%~dp0一键启动.bat"
set "NAPCAT_BAT=D:\AI\NapCat\启动NapCat.bat"
set "SNOWLUMA_BAT=%~dp0启动SnowLuma.bat"
set "BOT_BAT=%~dp0启动QQ机器人.bat"

rem ---------------------------------------------------------------------------
rem  协议端二选一（NapCat / SnowLuma）。两者 OneBot 端口完全一样（3000 HTTP /
rem  3001 WS），只有 WebUI 口不同（6099 / 5099），所以切换只影响：启动哪个 bat、
rem  探活哪个口、以及怎么出扫码窗口。
rem  [!!] 改这里之后，.env 里的 QQ_WEBUI_PORT 要跟着改 —— 看门狗靠它区分
rem       「进程没起来」和「起来了但没登录」，探错口就永远判成「进程死了」。
rem  传参：任意位置写 snowluma 都能切（force snowluma / force auto snowluma）；
rem      环境变量 QQ_PROTOCOL=snowluma、.env 的 QQ_WEBUI_PORT=5099 亦可。
rem ---------------------------------------------------------------------------
set "PROTOCOL=napcat"
rem 三个入口任意一个说 snowluma 就算：命令行参数（任意位置）、QQ_PROTOCOL
rem 环境变量、.env 里的 QQ_WEBUI_PORT。
rem [!!] 只认 %~3 是错的：手打「force snowluma」时 snowluma 落在 %~2，
rem      2026-09-30 就因为这一条白跑了半天 NapCat。
for %%a in (%~1 %~2 %~3) do if /i "%%~a"=="snowluma" set "PROTOCOL=snowluma"
if /i "%QQ_PROTOCOL%"=="snowluma" set "PROTOCOL=snowluma"
set "_ENV_PORT="
rem [!!] 读 .env **必须**用 findstr，不能用 for /f + usebackq：本项目的 .env 是
rem      **纯 LF**（0 个 CRLF），cmd 的 for /f 会把整篇当一行，%%a 永远匹配不上
rem      —— 09-30 实测 for/f 得到空、findstr 得到 5099。别改回 for /f。
if exist "%~dp0.env" (
    for /f "tokens=1,* delims==" %%a in ('findstr /i /b "QQ_WEBUI_PORT=" "%~dp0.env"') do set "_ENV_PORT=%%~b"
)
if "%_ENV_PORT%"=="5099" set "PROTOCOL=snowluma"

if /i "%PROTOCOL%"=="snowluma" (
    set "PROTO_BAT=%SNOWLUMA_BAT%"
    set "PROTO_PORT=5099"
    set "PROTO_NAME=SnowLuma"
) else (
    set "PROTO_BAT=%NAPCAT_BAT%"
    set "PROTO_PORT=6099"
    set "PROTO_NAME=NapCat"
)

set "FORCE_RESTART="
if /i "%~1"=="force" set "FORCE_RESTART=1"

rem auto = 看门狗调起来的（隐藏窗口跑）：跑完直接退出，不等回车、不留窗口
set "AUTO_RUN="
if /i "%~2"=="auto" set "AUTO_RUN=1"

echo ==================================================
echo   全部启动 : Agent 网页 + %PROTO_NAME% + QQ 适配层
echo ==================================================
echo.
echo   【注意】会强制结束 QQ 客户端
echo   协议端是注入到 QQ 进程里的，运行期不能同时开另一个 QQ
echo.

if not exist "%WEB_BAT%" (
    echo [ERROR] 找不到 "%WEB_BAT%"
    if not defined AUTO_RUN pause
    exit /b 1
)
if not exist "%PROTO_BAT%" (
    echo [ERROR] 找不到 "%PROTO_BAT%"
    if not defined AUTO_RUN pause
    exit /b 1
)
if not exist "%BOT_BAT%" (
    echo [ERROR] 找不到 "%BOT_BAT%"
    if not defined AUTO_RUN pause
    exit /b 1
)

rem 协议端已在跑说明小号已登录，重启要重新扫码 -> 跳过它，只重启网页端和适配层
set "PROTO_RUNNING="
for /f %%a in ('netstat -ano 2^>nul ^| findstr ":%PROTO_PORT% " ^| findstr "LISTENING"') do set "PROTO_RUNNING=1"

echo [1/2] 清理旧进程
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":5174 " ^| findstr "LISTENING"') do (
    echo       结束网页端 PID %%a
    taskkill /PID %%a /F >nul 2>&1
)
rem 关掉上一次的网页端窗口（cmd 会跟着命令改名，所以 一键启动.bat 里显式 title 了）
rem 不带 /T：那个窗口里还挂着 start 拉起来的浏览器，别顺手把人家的浏览器关了
taskkill /FI "WINDOWTITLE eq AGENT-WEB" /F >nul 2>&1
tasklist /FI "WINDOWTITLE eq QQBOT-ADAPTER" 2>nul | findstr /i "cmd.exe" >nul
if not errorlevel 1 (
    taskkill /FI "WINDOWTITLE eq QQBOT-ADAPTER" /T /F >nul 2>&1
    echo       结束旧的 QQ 适配层窗口
)

if defined FORCE_RESTART (
    echo       [force] 强制重启 %PROTO_NAME%（忽略 %PROTO_PORT% 监听状态）
    for %%p in (%PROTO_PORT% 3000 3001) do (
        for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":%%p " ^| findstr "LISTENING"') do (
            echo       结束端口 %%p 的进程 PID %%a
            taskkill /PID %%a /F >nul 2>&1
        )
    )
    rem 关掉上一次的 NapCat 窗口（进程杀完窗口会停在提示符，不清就一直堆）
    taskkill /FI "WINDOWTITLE eq NapCat - QQ 协议端" /T /F >nul 2>&1
    taskkill /FI "WINDOWTITLE eq SNOWLUMA" /T /F >nul 2>&1
    taskkill /IM QQ.exe /F >nul 2>&1
    taskkill /IM NapCatWinBootMain.exe /F >nul 2>&1
) else if defined PROTO_RUNNING (
    echo       %PROTO_NAME% 已在运行，跳过 - 重启它需要重新扫码
) else (
    for %%p in (%PROTO_PORT% 3000 3001) do (
        for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":%%p " ^| findstr "LISTENING"') do (
            echo       结束端口 %%p 的进程 PID %%a
            taskkill /PID %%a /F >nul 2>&1
        )
    )
    rem 关掉上一次的 NapCat 窗口（进程杀完窗口会停在提示符，不清就一直堆）
    taskkill /FI "WINDOWTITLE eq NapCat - QQ 协议端" /T /F >nul 2>&1
    taskkill /FI "WINDOWTITLE eq SNOWLUMA" /T /F >nul 2>&1
    taskkill /IM QQ.exe /F >nul 2>&1
    taskkill /IM NapCatWinBootMain.exe /F >nul 2>&1
)
ping -n 5 127.0.0.1 >nul
echo       完成
echo.

echo [2/2] 启动
start "AGENT-WEB" cmd /k "%WEB_BAT%"
echo       Agent Web        网页端  http://localhost:5174
if defined FORCE_RESTART (
    start "%PROTO_NAME%" cmd /k "%PROTO_BAT%"
    echo       %PROTO_NAME%           QQ 协议端 [force]
    ping -n 4 127.0.0.1 >nul
) else if defined PROTO_RUNNING (
    echo       %PROTO_NAME%           已在运行，本次不动
) else (
    start "%PROTO_NAME%" cmd /k "%PROTO_BAT%"
    echo       %PROTO_NAME%           QQ 协议端
    ping -n 4 127.0.0.1 >nul
)
start "QQBOT-ADAPTER" cmd /k "%BOT_BAT%"
ping -n 3 127.0.0.1 >nul
if /i "%PROTOCOL%"=="snowluma" (
    rem SnowLuma 不落盘 qrcode.png，码在 WebUI(5099) 里画 —— 开个页面代替扫码窗口
    rem 首次要用控制台的一次性临时密码登录，之后浏览器会记住
    start "" "http://127.0.0.1:5099"
) else (
    start "扫码窗口" cmd /k "%PY%" "%~dp0napcat_qr.py" --tries 12 --wait 120
)
echo       QQBOT-ADAPTER    QQ 适配层
echo.

echo ==================================================
echo [3/3] 启动看门狗
tasklist /FI "WINDOWTITLE eq WATCHDOG" 2>nul | findstr /i "cmd.exe" >nul
if not errorlevel 1 (
    echo       WATCHDOG         已在运行，本次不动
) else (
    start "WATCHDOG" cmd /k "%~dp0启动看门狗.bat"
    echo       WATCHDOG         心跳看门狗
)
echo.
echo   需要扫码登录时:
if /i "%PROTOCOL%"=="snowluma" (
echo   浏览器打开  http://127.0.0.1:5099
echo   密码 SnowLuma@2026（启动SnowLuma.bat 里写死的，只听 127.0.0.1）
echo   OneBot 端口与令牌已预置在 config\onebot.json，进去只要扫码
) else (
echo   浏览器打开  http://127.0.0.1:6099/webui
echo   用手机 QQ 扫码，建议用小号
)
echo   登录成功后适配层会自动连上，无需重启任何东西
echo ==================================================
echo.
echo   新开的窗口请勿关闭。本窗口可以关。
if not defined AUTO_RUN pause
