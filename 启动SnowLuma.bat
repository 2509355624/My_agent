@echo off
cd /d "%~dp0"
title SNOWLUMA

rem ===========================================================================
rem  SnowLuma 协议端启动器（NapCat 的替代品，2026-09-30 起可切换）
rem
rem  [!!] 跟 NapCat 最大的区别：SnowLuma **不会自己拉起 QQ**。
rem       它只做「发现 QQ.exe -> 注入 hook」，打包里一个 spawn 都没有。
rem       所以这里必须先保证 QQ.exe 在跑，否则 SnowLuma 起来了也是空的，
rem       WebUI 里永远显示「未检测到 QQ 进程」。
rem
rem  下面几个环境变量都是 SnowLuma 官方支持的，用来省掉首次交互:
rem    SNOWLUMA_ACCEPT_EULA=1 / SNOWLUMA_ACCEPT_PRIVACY=1   免掉协议确认弹窗
rem    SNOWLUMA_HOOK_AUTOLOAD=1                             自动注入（默认是关的！）
rem    SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD=...                固定 WebUI 登录密码
rem    SNOWLUMA_UPDATE_CHECK=0                              别去查 GitHub 更新
rem
rem  数据目录（config/ data/）是**相对当前工作目录**的，所以必须 pushd 进来。
rem ===========================================================================

set "SL_DIR=D://AI//SnowLuma"
set "SL_NODE=%SL_DIR%\node.exe"
set "SL_MAIN=%SL_DIR%\index.mjs"
set "QQ_EXE=D://APP//qq//QQ.exe"

rem auto = 看门狗调起来的（隐藏窗口跑）：跑完直接退出，不等回车
set "AUTO_RUN="
if /i "%~1"=="auto" set "AUTO_RUN=1"

if not exist "%SL_NODE%" (
    echo [ERROR] 找不到 "%SL_NODE%"
    echo         SnowLuma 未安装或路径不对（应从 GitHub Releases 解压到 %SL_DIR%）
    if not defined AUTO_RUN pause
    exit /b 1
)
if not exist "%SL_MAIN%" (
    echo [ERROR] 找不到 "%SL_MAIN%"
    echo         解压不完整，重新下载 SnowLuma-v*-win-x64.zip 解压到 %SL_DIR%
    if not defined AUTO_RUN pause
    exit /b 1
)

set SNOWLUMA_ACCEPT_EULA=1
set SNOWLUMA_ACCEPT_PRIVACY=1
set SNOWLUMA_HOOK_AUTOLOAD=1
set SNOWLUMA_UPDATE_CHECK=0
set "SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD=SnowLuma@2026"

echo ==================================================
echo   SnowLuma 启动器  ^(OneBot 11^)
echo ==================================================
echo.
echo   WebUI:  http://127.0.0.1:5099    登录密码 SnowLuma@2026
echo   WS:     127.0.0.1:3001   ^(事件上行^)
echo   HTTP:   127.0.0.1:3000   ^(消息下发^)
echo.
echo   提示: 3000 / 3001 只在 QQ 登录成功后才监听，
echo         登录前只有 5099，属正常现象。
echo.

tasklist /FI "IMAGENAME eq QQ.exe" /NH 2>nul | findstr /i "QQ.exe" >nul
if errorlevel 1 (
    echo   [0/2] QQ 没在跑，先拉起来 ^(SnowLuma 只注入、不拉 QQ^)
    if not exist "%QQ_EXE%" (
        echo         [ERROR] 找不到 "%QQ_EXE%"
        echo                 改本文件里的 QQ_EXE 指向你的 QQ 安装路径
        if not defined AUTO_RUN pause
        exit /b 1
    )
    start "" "%QQ_EXE%"
    echo         等 QQ 窗口就绪 ^(12 秒^) ...
    ping -n 13 127.0.0.1 >nul
) else (
    echo   [0/2] QQ 已在运行，SnowLuma 会自动发现并注入
)

echo   [1/2] 启动 SnowLuma ...   ^(Ctrl+C 退出^)
echo         首次登录：浏览器打开 http://127.0.0.1:5099 扫码。
echo.
pushd "%SL_DIR%"
"%SL_NODE%" "%SL_MAIN%"
popd

if not defined AUTO_RUN pause
